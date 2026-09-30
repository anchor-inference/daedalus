package main

// install.sh with a release key: the signature is fetched, checked — over the tag and
// SHA256SUMS together — before anything from the release is unpacked or run, and put beside
// SHA256SUMS for the bridge. The key is one this test makes and throws away; the installer copy it
// runs has that key written into its release_keys line, exactly where a real key would go. The
// "launcher" in the fixture release is a script that records whether it ran and what it was given.

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"
	"testing"
)

type sigRelease struct {
	tag       string
	sums      []byte // what is served as SHA256SUMS
	sig       []byte // what is served as SHA256SUMS.sig, nil for none
	archive   []byte
	report    string // where the fixture launcher in archive records that it ran
	requested []string
}

func (r *sigRelease) serve(t *testing.T) *httptest.Server {
	asset, _ := platformAsset(runtime.GOOS, runtime.GOARCH)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		r.requested = append(r.requested, req.URL.Path)
		switch req.URL.Path {
		case "/releases":
			json.NewEncoder(w).Encode([]map[string]any{{"tag_name": r.tag, "draft": false, "prerelease": false}})
		case "/download/" + r.tag + "/" + asset:
			w.Write(r.archive)
		case "/download/" + r.tag + "/SHA256SUMS":
			w.Write(r.sums)
		case "/download/" + r.tag + "/SHA256SUMS.sig":
			if r.sig == nil {
				http.NotFound(w, req)
				return
			}
			w.Write(r.sig)
		default:
			http.NotFound(w, req)
		}
	}))
	t.Cleanup(server.Close)
	return server
}

// keyLine is the base64 line of a signify record.
func keyLine(record string) string {
	for _, line := range strings.Split(record, "\n") {
		if line != "" && !strings.HasPrefix(line, "untrusted comment:") {
			return line
		}
	}
	return ""
}

// installerWithKey is install.sh with its release_keys line holding the test's key in place of the
// project's, exactly where the project's is written.
func installerWithKey(t *testing.T, key string) string {
	body, err := os.ReadFile("install.sh")
	if err != nil {
		t.Fatal(err)
	}
	line := regexp.MustCompile(`\nrelease_keys="[^"\n]*"\n`)
	if len(line.FindAllString(string(body), -1)) != 1 {
		t.Fatal("install.sh has no single release_keys line to fill")
	}
	patched := line.ReplaceAllLiteralString(string(body), "\nrelease_keys=\""+key+"\"\n")
	path := filepath.Join(t.TempDir(), "install.sh")
	os.WriteFile(path, []byte(patched), 0o755)
	return path
}

type sigCase struct {
	release  *sigRelease
	report   string
	target   string
	out      string
	ranAtAll bool
}

// runSigInstaller runs an installer over a v0.12.0 installation (data beside a launcher without
// upgrade), so a release that gets through is run as the bridge.
func runSigInstaller(t *testing.T, installer string, release *sigRelease, path string) sigCase {
	t.Helper()
	if release.report == "" {
		release.report = filepath.Join(t.TempDir(), "bridge-ran")
	}
	report := release.report
	os.Remove(report)
	bridge := "#!/bin/sh\n" +
		"if [ \"$1\" = --version ]; then echo " + release.tag + "; exit 0; fi\n" +
		"if [ \"$1\" = --help ]; then echo '  upgrade   x'; exit 0; fi\n" +
		"sums=''; while [ $# -gt 0 ]; do [ \"$1\" = --sums ] && sums=\"$2\"; shift; done\n" +
		"if [ -f \"$sums.sig\" ]; then echo sig-present >" + report + "; else echo sig-missing >" + report + "; fi\nexit 0\n"
	asset, _ := platformAsset(runtime.GOOS, runtime.GOARCH)
	if release.archive == nil {
		release.archive = makeArchive(t, asset, []archiveEntry{{name: "daedalus-desktop", body: bridge}})
	}
	if release.sums == nil {
		release.sums = []byte(fmt.Sprintf("%s  %s\n", hexSum(release.archive), asset))
	}
	server := release.serve(t)
	target := filepath.Join(t.TempDir(), "Daedalus")
	os.MkdirAll(filepath.Join(target, "data"), 0o700)
	os.WriteFile(filepath.Join(target, "daedalus-desktop"), []byte("#!/bin/sh\necho desktop-v0.12.0\n"), 0o755)
	cmd := exec.Command("sh", installer)
	cmd.Env = append(os.Environ(), "DAEDALUS_RELEASES_API="+server.URL, "DAEDALUS_DOWNLOAD_BASE="+server.URL+"/download",
		"DAEDALUS_DIR="+target, "DAEDALUS_UPGRADE_YES=1")
	if path != "" {
		cmd.Env = append(cmd.Env, "PATH="+path)
	}
	out, _ := cmd.CombinedOutput()
	ran, _ := os.ReadFile(report)
	return sigCase{release: release, report: strings.TrimSpace(string(ran)), target: target, out: string(out), ranAtAll: len(ran) > 0}
}

func requireInstallerTools(t *testing.T) {
	if runtime.GOOS != "linux" {
		t.Skip("the fixture archive is the Linux tarball")
	}
	for _, tool := range []string{"curl", "openssl"} {
		if _, err := exec.LookPath(tool); err != nil {
			t.Skip("no " + tool)
		}
	}
}

func TestTheInstallerChecksTheSignatureBeforeItRunsAnything(t *testing.T) {
	requireInstallerTools(t)
	record, sign := testKey(t, "key00001")
	_, signOther := testKey(t, "key00001") // same id, another key: a forgery naming the right key
	installer := installerWithKey(t, keyLine(record))

	good := &sigRelease{tag: "desktop-v0.13.0"}
	c := runSigInstaller(t, installer, good, "") // fills archive and sums; the first run has no signature
	if c.ranAtAll || !strings.Contains(c.out, "only installs signed releases") {
		t.Fatalf("an unsigned release was run:\n%s", c.out)
	}

	good = &sigRelease{tag: "desktop-v0.13.0", archive: c.release.archive, sums: c.release.sums, report: c.release.report}
	good.sig = sign(releaseMessage(good.tag, good.sums))
	c = runSigInstaller(t, installer, good, "")
	fingerprint := sha256.Sum256([]byte(keyLine(record)))
	if !strings.Contains(c.out, "Release key fingerprint (SHA-256 of the key line): "+hex.EncodeToString(fingerprint[:])) {
		t.Fatalf("the installer did not print its key's fingerprint:\n%s", c.out)
	}
	if c.report != "sig-present" || !strings.Contains(c.out, "signature verifies") {
		t.Fatalf("a signed release was not run, or run without its signature beside it (%q):\n%s", c.report, c.out)
	}

	// minisign -S -l writes its signature line and then a trusted comment and a second signature over
	// that comment: the installer reads the signature line alone.
	minisigned := &sigRelease{tag: good.tag, archive: good.archive, report: good.report, sums: good.sums,
		sig: append(append([]byte{}, good.sig...), []byte("trusted comment: timestamp:1790778020\tfile:SHA256SUMS\n"+
			"p4XDCnJgQcU1RcCt1Ua5kwJEr5jPhiRHs3rO4G05VN0XEZDuVBv8VYOPNmWc9umAD/xnVflENZJUqzbLiDwLCw==\n")...)}
	if c = runSigInstaller(t, installer, minisigned, ""); c.report != "sig-present" || !strings.Contains(c.out, "signature verifies") {
		t.Fatalf("a signature in minisign's layout was not taken (%q):\n%s", c.report, c.out)
	}

	for name, bad := range map[string]*sigRelease{
		"signed by another key": {tag: good.tag, archive: good.archive, report: good.report, sums: good.sums, sig: signOther(releaseMessage(good.tag, good.sums))},
		// An old release's signature, published again under a newer tag.
		"signed for another tag":           {tag: "desktop-v0.14.0", archive: good.archive, sums: good.sums, sig: sign(releaseMessage("desktop-v0.13.0", good.sums))},
		"SHA256SUMS changed after signing": {tag: good.tag, archive: good.archive, report: good.report, sums: append(append([]byte{}, good.sums...), []byte("0000  extra\n")...), sig: good.sig},
		"not a signature":                  {tag: good.tag, archive: good.archive, report: good.report, sums: good.sums, sig: []byte("untrusted comment: x\nnot base64\n")},
		"a prehashed signature":            {tag: good.tag, archive: good.archive, report: good.report, sums: good.sums, sig: prehashed(t, good.sig)},
	} {
		c := runSigInstaller(t, installer, bad, "")
		if c.ranAtAll {
			t.Errorf("%s: the release's launcher was run\n%s", name, c.out)
		}
		if !strings.Contains(c.out, "Nothing was installed") {
			t.Errorf("%s: no refusal\n%s", name, c.out)
		}
		if name == "a prehashed signature" && !strings.Contains(c.out, "prehashed") {
			t.Errorf("a prehashed signature is not named as such:\n%s", c.out)
		}
	}
}

// prehashed is a signature record with minisign's prehashed algorithm bytes ("ED") in place of the
// legacy ones ("Ed"), the rest as it was.
func prehashed(t *testing.T, sig []byte) []byte {
	t.Helper()
	line := keyLine(string(sig))
	raw, err := base64.StdEncoding.DecodeString(line)
	if err != nil || len(raw) != 74 {
		t.Fatalf("not a signature: %v", err)
	}
	raw[0], raw[1] = 'E', 'D'
	return []byte("untrusted comment: prehashed\n" + base64.StdEncoding.EncodeToString(raw) + "\n")
}

// With a key and no verifier on the machine, the installer refuses: it never falls back to trusting
// the release.
func TestWithAKeyAndNoVerifierTheInstallerRefuses(t *testing.T) {
	requireInstallerTools(t)
	record, sign := testKey(t, "key00001")
	installer := installerWithKey(t, keyLine(record))
	bin := t.TempDir()
	for _, tool := range []string{"sh", "curl", "uname", "grep", "sed", "awk", "tr", "sort", "tail", "head", "cut", "mkdir", "mktemp", "rm", "cat", "wc", "dd", "od", "sha256sum", "tar", "chmod", "pgrep", "date", "mv", "printf", "readlink", "dirname", "basename", "lsof"} {
		if path, err := exec.LookPath(tool); err == nil {
			os.Symlink(path, filepath.Join(bin, tool))
		}
	}
	first := runSigInstaller(t, installer, &sigRelease{tag: "desktop-v0.13.0"}, bin)
	release := &sigRelease{tag: "desktop-v0.13.0", archive: first.release.archive, sums: first.release.sums, report: first.release.report}
	release.sig = sign(releaseMessage(release.tag, release.sums))
	c := runSigInstaller(t, installer, release, bin)
	if c.ranAtAll || !strings.Contains(c.out, "needs OpenSSL 3") {
		t.Fatalf("installed without being able to check the signature:\n%s", c.out)
	}
}

// The bridge with a release key compiled in, as the installer now delivers it: the signature beside
// SHA256SUMS, over this tag.
func TestWithAKeyTheBridgeTakesTheReleaseTheInstallerDelivers(t *testing.T) {
	record, sign := testKey(t, "key00001")
	old := trustedReleaseKeys
	trustedReleaseKeys = []string{record}
	t.Cleanup(func() { trustedReleaseKeys = old })
	b := newBridge(t, newRelease())
	sums, _ := os.ReadFile(b.sums)
	os.WriteFile(b.sums+".sig", sign(releaseMessage("desktop-v0.13.0", sums)), 0o644)
	if err := b.u.Run(context.Background()); err != nil || b.in.file(t, launcherName()) != "new launcher" {
		t.Fatalf("%v\n%s", err, b.u.out)
	}
}

func TestWithAKeyTheBridgeRefusesASignatureForAnotherTag(t *testing.T) {
	record, sign := testKey(t, "key00001")
	old := trustedReleaseKeys
	trustedReleaseKeys = []string{record}
	t.Cleanup(func() { trustedReleaseKeys = old })
	b := newBridge(t, newRelease())
	sums, _ := os.ReadFile(b.sums)
	os.WriteFile(b.sums+".sig", sign(releaseMessage("desktop-v0.12.9", sums)), 0o644)
	if err := b.u.Run(context.Background()); err == nil {
		t.Fatal("an older release's signature was taken for this one")
	}
	b.unchanged(t)
}

func hexSum(body []byte) string {
	sum := sha256.Sum256(body)
	return hex.EncodeToString(sum[:])
}

// A launcher started from its own folder as ./daedalus-desktop has no absolute path in its command
// line. The installer must still see it running and change nothing: matching the path in command
// lines (pgrep -f) missed it, and the bridge then replaced the files of a running launcher.
func TestTheInstallerSeesALauncherStartedByARelativePath(t *testing.T) {
	requireInstallerTools(t)
	sleep, err := exec.LookPath("sleep")
	if err != nil {
		t.Skip("no sleep")
	}
	program, err := os.ReadFile(sleep)
	if err != nil {
		t.Fatal(err)
	}
	record, sign := testKey(t, "key00001")
	installer := installerWithKey(t, keyLine(record))
	release := &sigRelease{tag: "desktop-v0.13.0"}
	release.report = filepath.Join(t.TempDir(), "bridge-ran")
	asset, _ := platformAsset(runtime.GOOS, runtime.GOARCH)
	release.archive = makeArchive(t, asset, []archiveEntry{{name: "daedalus-desktop", body: "#!/bin/sh\necho ran >" + release.report + "\n"}})
	release.sums = []byte(fmt.Sprintf("%s  %s\n", hexSum(release.archive), asset))
	release.sig = sign(releaseMessage(release.tag, release.sums))
	server := release.serve(t)
	target := filepath.Join(t.TempDir(), "Daedalus")
	os.MkdirAll(filepath.Join(target, "data"), 0o700)
	// A real program, so that what it runs is the launcher's file; a script would be run by sh.
	if err := os.WriteFile(filepath.Join(target, "daedalus-desktop"), program, 0o755); err != nil {
		t.Fatal(err)
	}
	running := exec.Command("./daedalus-desktop", "60")
	running.Dir = target
	if err := running.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { running.Process.Kill(); running.Wait() })
	cmd := exec.Command("sh", installer)
	cmd.Env = append(os.Environ(), "DAEDALUS_RELEASES_API="+server.URL, "DAEDALUS_DOWNLOAD_BASE="+server.URL+"/download",
		"DAEDALUS_DIR="+target, "DAEDALUS_UPGRADE_YES=1")
	out, err := cmd.CombinedOutput()
	if err == nil || !strings.Contains(string(out), "is running") {
		t.Fatalf("the installer went on over a running launcher (%v):\n%s", err, out)
	}
	if _, err := os.Stat(release.report); err == nil {
		t.Fatal("the bridge ran over a running launcher")
	}
}
