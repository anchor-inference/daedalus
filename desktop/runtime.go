package main

// The portable runtime is what a native installation has instead of an image: a folder under the
// data directory holding the few binaries the agent cannot work without, each downloaded from its
// publisher, checked against a hash written down here, and unpacked where nothing else on the
// machine can see it. Nothing is installed system-wide, no package manager is run, and no PATH is
// changed — uninstalling is deleting the folder.
//
// Every version below is pinned and every hash was read from the publisher's own checksum file at
// the time it was written down. A download whose hash does not match is not used: it is deleted and
// the start fails saying so, because a binary that is not the one that was pinned is not a binary
// this installation runs.

import (
	"archive/tar"
	"archive/zip"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

// download is one archive the runtime is made of.
type download struct {
	name    string
	version string
	url     string
	sha256  string
	size    int64
	// kind is "tar.gz" or "zip" — the two formats the standard library can open. Node publishes a
	// smaller .tar.xz and it is deliberately not used: xz would be the module's second dependency,
	// paid on every platform to save twenty megabytes on one optional extra.
	kind string
	// strip is how many leading path segments of every entry belong to the archive rather than to
	// the tree inside it. Publishers wrap their trees in a folder named after the release.
	strip int
	// only, when it is not empty, is the whole of what is taken out of the archive: the runtime
	// wants a binary, not a copy of the project's documentation and shell completions.
	only []string
	// exec names the entries that must come out executable. A tar carries its modes and a zip on
	// Windows does not need them, but a zip unpacked on a POSIX machine does.
	exec []string
}

// uvVersion and the tables below are the pinned runtime. Raising a version means replacing the
// hashes with the ones the new release publishes, in the same commit: a version without its hash
// is a download nobody checked.
const (
	uvVersion      = "0.12.15"
	ripgrepVersion = "15.2.0"
	nodeVersion    = "24.21.0"
	minGitVersion  = "2.55.0.5"
	// The tag the MinGit archives hang off; it spells the same release differently from the file names.
	minGitTag     = "v2.55.0.windows.5"
	pythonVersion = "3.12"
	// The build of pythonVersion that uv uvVersion installs, and the python-build-standalone release
	// it comes from. They move with uv: a new uv may pick a newer patch, and the table below must then
	// name the file it picks, or the copy carried in the installation is passed over and the
	// interpreter downloaded as before (`make-seed` checks the two agree when it builds a release).
	pythonBuild   = "3.12.14"
	pythonRelease = "20260901"
)

// uvDownloads: https://github.com/astral-sh/uv/releases/download/<version>/sha256.sum
var uvDownloads = map[string]download{
	"linux/amd64": {
		name: "uv", version: uvVersion, kind: "tar.gz", strip: 1, only: []string{"uv"}, exec: []string{"uv"},
		url:    "https://github.com/astral-sh/uv/releases/download/" + uvVersion + "/uv-x86_64-unknown-linux-gnu.tar.gz",
		sha256: "f97935763c04be3e692460a7aaeaaab8fc3b78fcf8b389da820b38ae7423a638", size: 19443011,
	},
	"linux/arm64": {
		name: "uv", version: uvVersion, kind: "tar.gz", strip: 1, only: []string{"uv"}, exec: []string{"uv"},
		url:    "https://github.com/astral-sh/uv/releases/download/" + uvVersion + "/uv-aarch64-unknown-linux-gnu.tar.gz",
		sha256: "0e9a3499b0587d449c9ff684c0160da607826e4af1cee220bc87f378702d3e08", size: 18654404,
	},
	"darwin/arm64": {
		name: "uv", version: uvVersion, kind: "tar.gz", strip: 1, only: []string{"uv"}, exec: []string{"uv"},
		url:    "https://github.com/astral-sh/uv/releases/download/" + uvVersion + "/uv-aarch64-apple-darwin.tar.gz",
		sha256: "dc304b9ed1b24174572290fba60ac3f6fe63c73a671f0439e62a91375841964d", size: 16678128,
	},
	"darwin/amd64": {
		name: "uv", version: uvVersion, kind: "tar.gz", strip: 1, only: []string{"uv"}, exec: []string{"uv"},
		url:    "https://github.com/astral-sh/uv/releases/download/" + uvVersion + "/uv-x86_64-apple-darwin.tar.gz",
		sha256: "e9ca61775532368fe518ab03e7a354c7ecab8ccb3c7d941c775fcc4a362b801b", size: 20292831,
	},
	"windows/amd64": {
		name: "uv", version: uvVersion, kind: "zip", only: []string{"uv.exe"},
		url:    "https://github.com/astral-sh/uv/releases/download/" + uvVersion + "/uv-x86_64-pc-windows-msvc.zip",
		sha256: "477bd99a84e34891f2bd4c9152ddeb74e971accccbc59c0f0301f11f08a32d46", size: 17578593,
	},
	"windows/arm64": {
		name: "uv", version: uvVersion, kind: "zip", only: []string{"uv.exe"},
		url:    "https://github.com/astral-sh/uv/releases/download/" + uvVersion + "/uv-aarch64-pc-windows-msvc.zip",
		sha256: "a37c8e96cb1260488c8510b64c848533a3a82a2fdf9e905de7c2700ceebf6437", size: 18880214,
	},
}

// ripgrepDownloads: each release asset's own .sha256 file. The Linux builds are the musl ones: they
// are static, so the one binary runs on every distribution the launcher may land on.
var ripgrepDownloads = map[string]download{
	"linux/amd64": {
		name: "rg", version: ripgrepVersion, kind: "tar.gz", strip: 1, only: []string{"rg"}, exec: []string{"rg"},
		url:    "https://github.com/BurntSushi/ripgrep/releases/download/" + ripgrepVersion + "/ripgrep-" + ripgrepVersion + "-x86_64-unknown-linux-musl.tar.gz",
		sha256: "33e15bcf1624b25cdd2a55813a47a2f95dbe126268203e76aa6a585d1e7b149c", size: 2265718,
	},
	"linux/arm64": {
		name: "rg", version: ripgrepVersion, kind: "tar.gz", strip: 1, only: []string{"rg"}, exec: []string{"rg"},
		url:    "https://github.com/BurntSushi/ripgrep/releases/download/" + ripgrepVersion + "/ripgrep-" + ripgrepVersion + "-aarch64-unknown-linux-musl.tar.gz",
		sha256: "800b1e7206afe799dfb5a6901f23147cfaabe0e52210538100f61e86e1740915", size: 1982561,
	},
	"darwin/arm64": {
		name: "rg", version: ripgrepVersion, kind: "tar.gz", strip: 1, only: []string{"rg"}, exec: []string{"rg"},
		url:    "https://github.com/BurntSushi/ripgrep/releases/download/" + ripgrepVersion + "/ripgrep-" + ripgrepVersion + "-aarch64-apple-darwin.tar.gz",
		sha256: "3750b2e93f37e0c692657da574d7019a101c0084da05a790c83fd335bad973e4", size: 1764284,
	},
	"darwin/amd64": {
		name: "rg", version: ripgrepVersion, kind: "tar.gz", strip: 1, only: []string{"rg"}, exec: []string{"rg"},
		url:    "https://github.com/BurntSushi/ripgrep/releases/download/" + ripgrepVersion + "/ripgrep-" + ripgrepVersion + "-x86_64-apple-darwin.tar.gz",
		sha256: "af7825fcc69a2afc7a7aea55fc9af90e26421d8f20fe59df32e233c0b8a231c1", size: 1878284,
	},
	"windows/amd64": {
		name: "rg", version: ripgrepVersion, kind: "zip", strip: 1, only: []string{"rg.exe"},
		url:    "https://github.com/BurntSushi/ripgrep/releases/download/" + ripgrepVersion + "/ripgrep-" + ripgrepVersion + "-x86_64-pc-windows-msvc.zip",
		sha256: "71b2fef860abe467217a538ff31de02f5258807c0129f771846f87bd029aafc5", size: 1789611,
	},
	"windows/arm64": {
		name: "rg", version: ripgrepVersion, kind: "zip", strip: 1, only: []string{"rg.exe"},
		url:    "https://github.com/BurntSushi/ripgrep/releases/download/" + ripgrepVersion + "/ripgrep-" + ripgrepVersion + "-aarch64-pc-windows-msvc.zip",
		sha256: "e4abca10c3a64ebea742667dd7009449d49403db5460dd6873e389fa2945360f", size: 1640124,
	},
}

// gitDownloads is Windows only, and it is MinGit — Git for Windows without the installer, the
// documentation or the GUI. Everywhere else git is a program the machine already has: macOS ships
// it with the Command Line Tools and every Linux desktop has it or is one package away, and a
// second copy of it downloaded from a third party is a copy nobody maintains.
//
// Hashes: the release notes of https://github.com/git-for-windows/git/releases/tag/v<...>.
var gitDownloads = map[string]download{
	"windows/amd64": {
		name: "git", version: minGitVersion, kind: "zip",
		url:    "https://github.com/git-for-windows/git/releases/download/" + minGitTag + "/MinGit-" + minGitVersion + "-64-bit.zip",
		sha256: "56d7b226b7693196cfc71fef26568f536c4a021ab6c37ff2db4287bed908e96e", size: 38989688,
	},
	"windows/arm64": {
		name: "git", version: minGitVersion, kind: "zip",
		url:    "https://github.com/git-for-windows/git/releases/download/" + minGitTag + "/MinGit-" + minGitVersion + "-arm64.zip",
		sha256: "05843f9d6e60306c3ab886799e2c67200caab921571f10512df3493049179ddb", size: 37650057,
	},
}

// pythonDownloads are the interpreters uv installs, pinned here only so that the copy a release
// carries can be checked before uv is pointed at it. uv still fetches and checks its own when there is
// no such copy — it has these hashes built in — so this table is the release's half of the bargain,
// not a second source of truth. The URLs are the publisher's own; uv mirrors them under another host.
//
// Hashes: https://github.com/astral-sh/python-build-standalone/releases/download/<release>/SHA256SUMS.
var pythonDownloads = map[string]download{
	"linux/amd64":   pythonArchive("x86_64-unknown-linux-gnu", "72748da13197c1fb161e3afeef20a6a385ff24f2165e6e2758e47008e7faba4c", 34143368),
	"linux/arm64":   pythonArchive("aarch64-unknown-linux-gnu", "577b4bec0793ad1ff0cbff9adbd0df078eddde38a4c41bf5d83ad381a85ee39d", 29199399),
	"darwin/amd64":  pythonArchive("x86_64-apple-darwin", "65b195c9cedc1fef6767f044f9822069adbd1bd9204d424ece4628776fdc04bb", 24686153),
	"darwin/arm64":  pythonArchive("aarch64-apple-darwin", "81a359f1cfadd4da11766534c5913791cea55f26e1bb902cacd2a531bb1e4b2b", 24981445),
	"windows/amd64": pythonArchive("x86_64-pc-windows-msvc", "7c45c9622400d578709a9b2cddbe8124cc21d382409d9f13406d706d28e31b14", 21980728),
	"windows/arm64": pythonArchive("aarch64-pc-windows-msvc", "72f4713d056a17961bdba7b43be82a878035040fa1eee30cfd5e43b91a2852d9", 20725445),
}

// pythonArchive is one row of pythonDownloads: the stripped install-only build, which is the one uv
// installs.
func pythonArchive(triple, sum string, size int64) download {
	file := "cpython-" + pythonBuild + "+" + pythonRelease + "-" + triple + "-install_only_stripped.tar.gz"
	return download{
		name: "python", version: pythonBuild, kind: "tar.gz", sha256: sum, size: size,
		url: "https://github.com/astral-sh/python-build-standalone/releases/download/" + pythonRelease + "/" + strings.ReplaceAll(file, "+", "%2B"),
	}
}

// pythonFile is the archive's file name as uv looks for it under a mirror: the release, then the
// name with its plus sign as it is, not as the URL escapes it.
func pythonFile(d download) string {
	file, _ := url.PathUnescape(path.Base(d.url))
	return file
}

// nodeDownloads is an extra, not part of a first run: four skills shell out to npx and the Mini App
// can be rebuilt with it, and an installation that does neither never pays for it.
// Hashes: https://nodejs.org/dist/v<version>/SHASUMS256.txt.
var nodeDownloads = map[string]download{
	"linux/amd64": {
		name: "node", version: nodeVersion, kind: "tar.gz", strip: 1,
		url:    "https://nodejs.org/dist/v" + nodeVersion + "/node-v" + nodeVersion + "-linux-x64.tar.gz",
		sha256: "6e1db87ef58b8819e5d5402eff1536491b18edd8eb7bee5ef7897876e88dc5ff", size: 58088022,
	},
	"linux/arm64": {
		name: "node", version: nodeVersion, kind: "tar.gz", strip: 1,
		url:    "https://nodejs.org/dist/v" + nodeVersion + "/node-v" + nodeVersion + "-linux-arm64.tar.gz",
		sha256: "724282c3b43aec998aa9527380465b45d229e021b58035f5f4f63095eabfe5d5", size: 57824078,
	},
	"darwin/arm64": {
		name: "node", version: nodeVersion, kind: "tar.gz", strip: 1,
		url:    "https://nodejs.org/dist/v" + nodeVersion + "/node-v" + nodeVersion + "-darwin-arm64.tar.gz",
		sha256: "bed7eea5325e1108f32ce5228ddd6a5f0f08a499ee42aa7442aea583702f6057", size: 52909993,
	},
	"darwin/amd64": {
		name: "node", version: nodeVersion, kind: "tar.gz", strip: 1,
		url:    "https://nodejs.org/dist/v" + nodeVersion + "/node-v" + nodeVersion + "-darwin-x64.tar.gz",
		sha256: "1462cb3b3046b815cf8ea436d3da450ec1a9f11dac7e5a46b0ada5305d7e8097", size: 54203979,
	},
	"windows/amd64": {
		name: "node", version: nodeVersion, kind: "zip", strip: 1,
		url:    "https://nodejs.org/dist/v" + nodeVersion + "/node-v" + nodeVersion + "-win-x64.zip",
		sha256: "158f7685b44de51f6c0df1d153526cbcd3e1bc739a8dfc607721cef75de9e541", size: 37618919,
	},
	"windows/arm64": {
		name: "node", version: nodeVersion, kind: "zip", strip: 1,
		url:    "https://nodejs.org/dist/v" + nodeVersion + "/node-v" + nodeVersion + "-win-arm64.zip",
		sha256: "8779b1bde1d39f8d420e3b57aa657b39891af434d3de44a919044cec06785921", size: 33679608,
	},
}

// platformKey is how the tables above are indexed.
func platformKey(goos, goarch string) string { return goos + "/" + goarch }

// pick reads a table for this machine. A platform that is not in a table is not a failure of the
// download: it is a platform the runtime has no pinned build for, and the caller says so in the
// terms of the thing that is missing.
func pick(table map[string]download, goos, goarch string) (download, error) {
	d, ok := table[platformKey(goos, goarch)]
	if !ok {
		return download{}, fmt.Errorf("no pinned build for %s", platformKey(goos, goarch))
	}
	return d, nil
}

// downloadLimit is the wall between a redirect to something enormous and a full disk. The largest
// pinned archive is MinGit at 39 MB.
const downloadLimit = 512 << 20

// httpsOnly is the client the pinned downloads use. http.DefaultClient follows a redirect from https
// to http without a word, which for a hashed archive turns an attack into a hash mismatch and for the
// checkout tarball — which has no hash — turns it into code that is unpacked and executed as the
// supervisor on the next start. A hop that leaves https is refused rather than followed.
//
// The host is deliberately not pinned here: a GitHub release asset redirects to
// release-assets.githubusercontent.com by design, and refusing that refuses every download the
// launcher makes. What stands behind these is the sha256 in the table beside the URL. The one
// download with no hash — the checkout tarball — gets onCodeload below instead.
var httpsOnly = &http.Client{CheckRedirect: refuseUnlessHTTPS("")}

// onCodeload is httpsOnly with the host pinned as well, for the download that is not hashed.
var onCodeload = &http.Client{CheckRedirect: refuseUnlessHTTPS("codeload.github.com")}

func refuseUnlessHTTPS(host string) func(*http.Request, []*http.Request) error {
	return func(req *http.Request, via []*http.Request) error {
		if len(via) >= 10 {
			return fmt.Errorf("%s: too many redirects", req.URL.Host)
		}
		if req.URL.Scheme != "https" {
			return fmt.Errorf("%s redirects to %s, which is not https", via[0].URL.Host, req.URL.Scheme)
		}
		if host != "" && req.URL.Host != host {
			return fmt.Errorf("%s redirects to %s, which is not %s", via[0].URL.Host, req.URL.Host, host)
		}
		return nil
	}
}

// fetchVerified downloads an archive and returns it only when its hash is the pinned one. The whole
// archive is held in memory and checked before a single byte of it is written anywhere: a runtime
// half-written from a download that turned out to be something else is worse than no runtime.
func fetchVerified(ctx context.Context, d download) ([]byte, error) {
	return fetchVerifiedReporting(ctx, d, nil)
}

// fetchVerifiedReporting is fetchVerified with the page told how far the download has got. The size
// is the pinned one, known before the first byte arrives.
func fetchVerifiedReporting(ctx context.Context, d download, report reporter) ([]byte, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, d.url, nil)
	if err != nil {
		return nil, err
	}
	response, err := httpsOnly.Do(request)
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("%s: HTTP %d", d.url, response.StatusCode)
	}
	total := d.size
	if total <= 0 && response.ContentLength > 0 {
		total = response.ContentLength
	}
	body, err := io.ReadAll(newCountingReader(io.LimitReader(response.Body, downloadLimit), Activity{Kind: "download", Name: d.label(), Total: total}, report))
	if err != nil {
		return nil, err
	}
	if err := d.verify(body); err != nil {
		return nil, fmt.Errorf("%s %w", d.url, err)
	}
	return body, nil
}

// verify checks an archive against the pinned hash, wherever it came from: the network or the copy a
// release carries in the installation.
func (d download) verify(body []byte) error {
	sum := sha256.Sum256(body)
	if got := hex.EncodeToString(sum[:]); got != d.sha256 {
		return fmt.Errorf("is not what was pinned: sha256 %s, expected %s", got, d.sha256)
	}
	return nil
}

// label is how a tool is named on the page and in the log.
func (d download) label() string { return d.name + " " + d.version }

// stripName drops the leading segments that belong to the archive and returns the name the entry
// has inside the tree. An entry with fewer segments than the archive wraps is the wrapper itself.
func stripName(name string, strip int) string {
	name = strings.TrimPrefix(path.Clean("/"+strings.TrimPrefix(name, "./")), "/")
	for i := 0; i < strip; i++ {
		_, rest, ok := strings.Cut(name, "/")
		if !ok {
			return ""
		}
		name = rest
	}
	return name
}

// wanted reports whether an entry is one of the ones to keep.
func (d download) wanted(name string) bool {
	if name == "" {
		return false
	}
	if len(d.only) == 0 {
		return true
	}
	for _, kept := range d.only {
		if name == kept {
			return true
		}
	}
	return false
}

// executable reports whether an entry has to come out with the execute bit on however the archive
// recorded it.
func (d download) executable(name string) bool {
	for _, e := range d.exec {
		if name == e {
			return true
		}
	}
	return false
}

// unpackInto writes the wanted entries of an archive under dir. Entry names are cleaned and every
// directory on the way is made one segment at a time without following a symbolic link — the same
// care the repository tarballs are unpacked with, for the same reason.
func unpackInto(d download, body []byte, dir string) error {
	return unpackIntoReporting(d, body, dir, nil)
}

// unpackIntoReporting is unpackInto with the page told how far it has got. MinGit is thousands of
// small files, and on Windows writing them is a step of its own that the page has to show moving.
func unpackIntoReporting(d download, body []byte, dir string, report reporter) error {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	switch d.kind {
	case "tar.gz":
		return unpackTarInto(d, body, dir, report)
	case "zip":
		return unpackZipInto(d, body, dir, report)
	default:
		return fmt.Errorf("unknown archive kind %q", d.kind)
	}
}

func unpackTarInto(d download, body []byte, dir string, report reporter) error {
	// A gzip stream has no table of contents, so how far through it is measured by how much of the
	// compressed archive has been read — which is honest, and needs no second pass over it.
	zipped, err := gzip.NewReader(newCountingReader(bytes.NewReader(body), Activity{Kind: "unpack", Name: d.label(), Total: int64(len(body))}, report))
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
		name := stripName(header.Name, d.strip)
		if !d.wanted(name) || header.Typeflag != tar.TypeReg {
			// Directories are made by the files that land in them and everything else a release
			// archive carries — links, the documentation tree — is not part of the runtime.
			continue
		}
		mode := os.FileMode(header.Mode).Perm()
		if d.executable(name) {
			mode |= 0o755
		}
		if err := writeEntry(dir, name, mode, reader); err != nil {
			return err
		}
	}
}

func unpackZipInto(d download, body []byte, dir string, report reporter) error {
	archive, err := zip.NewReader(bytes.NewReader(body), int64(len(body)))
	if err != nil {
		return err
	}
	// A zip knows its entries up front, so here the count is of the compressed bytes behind each
	// entry: the same unit as a tar's, and the bar does not race through a thousand empty folders.
	progress := Activity{Kind: "unpack", Name: d.label(), Total: int64(len(body)), Unit: unitBytes}
	report.report(progress)
	last := time.Now()
	defer func() { progress.Done = progress.Total; report.report(progress) }()
	for _, entry := range archive.File {
		progress.Done += int64(entry.CompressedSize64)
		if time.Since(last) >= progressEvery {
			last = time.Now()
			report.report(progress)
		}
		name := stripName(entry.Name, d.strip)
		// Regular files only, as on the tar path: a zip symlink entry holds its target as content,
		// so writing it out makes a file full of a path where a link was meant.
		if !d.wanted(name) || !entry.FileInfo().Mode().IsRegular() {
			continue
		}
		mode := entry.Mode().Perm()
		if mode == 0 {
			mode = 0o644
		}
		if d.executable(name) || strings.HasSuffix(name, ".exe") {
			mode |= 0o755
		}
		file, err := entry.Open()
		if err != nil {
			return err
		}
		err = writeEntry(dir, name, mode, file)
		file.Close()
		if err != nil {
			return err
		}
	}
	return nil
}

// writeEntry puts one file in its place under dir.
func writeEntry(dir, name string, mode os.FileMode, body io.Reader) error {
	target, err := safeJoin(dir, name)
	if err != nil {
		return err
	}
	if err := ensureParents(dir, filepath.Dir(target)); err != nil {
		return err
	}
	if info, err := os.Lstat(target); err == nil && info.Mode()&os.ModeSymlink != 0 {
		if err := os.Remove(target); err != nil {
			return err
		}
	}
	file, err := os.OpenFile(target, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, mode)
	if err != nil {
		return err
	}
	if _, err := io.Copy(file, io.LimitReader(body, downloadLimit)); err != nil {
		file.Close()
		return err
	}
	if err := file.Close(); err != nil {
		return err
	}
	// A file that already existed keeps its old mode through O_CREATE, so the mode is set rather
	// than asked for: a binary unpacked before a version bump must not stay unexecutable.
	return os.Chmod(target, mode)
}

// installed reports whether this exact version of a tool is already unpacked, by the stamp written
// beside the runtime after a successful install. The stamp carries the hash as well as the version:
// a pinned build whose hash was corrected is a different build.
func (p Paths) installed(d download) bool {
	body, err := os.ReadFile(p.stamp(d.name))
	return err == nil && strings.TrimSpace(string(body)) == d.version+" "+d.sha256
}

func (p Paths) stamp(name string) string { return filepath.Join(p.RuntimeStamps, name) }

func (p Paths) writeStamp(d download) error {
	if err := os.MkdirAll(p.RuntimeStamps, 0o755); err != nil {
		return err
	}
	return os.WriteFile(p.stamp(d.name), []byte(d.version+" "+d.sha256+"\n"), 0o644)
}

// installTool downloads, checks and unpacks one archive, and does nothing at all when the pinned
// version is already there. It returns how many bytes of archive it fetched — not the first-run
// total, which is mostly the uv-managed CPython and `uv sync` and is several times larger. The
// README's figure is measured on the folder afterwards, not on this.
func installTool(ctx context.Context, p Paths, d download, dir string, log func(string, ...any)) (int64, error) {
	return installToolFrom(ctx, p, d, dir, log, nil, nil)
}

// installToolFrom is installTool with the page told what is happening and, when the installation
// carries the archive itself (seed.go), the archive taken from there instead of the network. A
// carried archive is checked against the same pinned hash a download is: it came inside a signed
// release, and it is still not unpacked on that alone.
func installToolFrom(ctx context.Context, p Paths, d download, dir string, log func(string, ...any), report reporter, seed *Seed) (int64, error) {
	if p.installed(d) {
		return 0, nil
	}
	var body []byte
	var fetched int64
	if archive, ok := seed.runtimeArchive(d, report, log); ok {
		log("%s %s comes with the installation (%.1f MB)", d.name, d.version, float64(d.size)/1e6)
		body = archive
	} else {
		log("downloading %s %s (%.1f MB)", d.name, d.version, float64(d.size)/1e6)
		archive, err := fetchVerifiedReporting(ctx, d, report)
		if err != nil {
			return 0, err
		}
		body, fetched = archive, int64(len(archive))
	}
	if err := unpackIntoReporting(d, body, dir, report); err != nil {
		return 0, err
	}
	if err := p.writeStamp(d); err != nil {
		return 0, err
	}
	return fetched, nil
}

// systemGit is the git already on this machine, or an empty string. macOS keeps it inside the
// Command Line Tools, so `xcode-select -p` answering is what says it is really there: /usr/bin/git
// exists on a Mac with no tools installed and prompts for them when it is run.
func systemGit(goos string, lookPath func(string) (string, error), run func(string, ...string) error) string {
	found, err := lookPath("git")
	if err != nil {
		return ""
	}
	if goos == "darwin" {
		if err := run("xcode-select", "-p"); err != nil {
			return ""
		}
	}
	return found
}

// gitMissing is what an operator sees when native mode has no git to work with. git is how the
// checkouts are made and how the agent's own changes are recorded, so this is a refusal, not a
// warning, and it names the one command that fixes it rather than running a package manager itself.
func gitMissing(goos string) error {
	switch goos {
	case "darwin":
		return errors.New("native mode needs git, which on macOS comes with the Command Line Tools: run `xcode-select --install`, then start Daedalus again")
	default:
		return errors.New("native mode needs git: install it with your package manager (`sudo apt install git`, `sudo dnf install git`, `sudo pacman -S git`), then start Daedalus again")
	}
}

// commandRuns is systemGit's probe: a command that exits 0.
func commandRuns(name string, args ...string) error {
	return exec.Command(name, args...).Run()
}

// EnsureGit resolves the git this installation uses: MinGit under the runtime folder on Windows,
// the machine's own everywhere else.
func EnsureGit(ctx context.Context, p Paths, log func(string, ...any)) (string, int64, error) {
	return ensureGitFrom(ctx, p, log, nil, nil)
}

func ensureGitFrom(ctx context.Context, p Paths, log func(string, ...any), report reporter, seed *Seed) (string, int64, error) {
	if runtime.GOOS != "windows" {
		if found := systemGit(runtime.GOOS, exec.LookPath, commandRuns); found != "" {
			return found, 0, nil
		}
		return "", 0, gitMissing(runtime.GOOS)
	}
	d, err := pick(gitDownloads, runtime.GOOS, runtime.GOARCH)
	if err != nil {
		return "", 0, fmt.Errorf("git: %w", err)
	}
	bytes, err := installToolFrom(ctx, p, d, p.RuntimeGit, log, report, seed)
	if err != nil {
		return "", 0, err
	}
	return filepath.Join(p.RuntimeGit, "cmd", "git.exe"), bytes, nil
}
