package main

// desktop/sign-release.sh, run with gh faked on PATH (a script that serves one draft release and
// records what it was asked) and minisign either faked the same way — a stand-in, run as this test
// binary, that signs in minisign's legacy layout with a key the test made and refuses to sign
// without -l — or, when minisign is installed, the real one with a key made for the run. What it
// uploads is then checked with the launcher's own verifier.

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

const fakeGH = `#!/bin/sh
state="$FAKE_GH_STATE"
echo "$*" >>"$state/log"
case "$1 $2" in
  "release view")
    case "$*" in
      *isDraft*) cat "$state/draft" ;;
      *assets*) cat "$state/assets" ;;
    esac ;;
  "release download")
    dir=""; prev=""
    for a in "$@"; do [ "$prev" = --dir ] && dir="$a"; prev="$a"; done
    cp "$state/SHA256SUMS" "$dir/SHA256SUMS" ;;
  "release upload") cp "$4" "$state/uploaded.sig" ;;
  "release edit") echo "$*" >"$state/published" ;;
esac
`

// fakeMinisign is the stand-in's body: -S (with -l, or it refuses: the launcher cannot verify a
// prehashed signature) and -V, over a key file holding "<key id> <hex seed>".
func fakeMinisign(args []string) int {
	value := func(flag string) string {
		for i, a := range args {
			if a == flag && i+1 < len(args) {
				return args[i+1]
			}
		}
		return ""
	}
	has := func(flag string) bool {
		for _, a := range args {
			if a == flag {
				return true
			}
		}
		return false
	}
	message, err := os.ReadFile(value("-m"))
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	switch {
	case has("-S"):
		if !has("-l") {
			fmt.Fprintln(os.Stderr, "a prehashed signature: the launcher cannot verify it")
			return 1
		}
		keyFile := value("-s")
		if keyFile == "" {
			keyFile = filepath.Join(os.Getenv("HOME"), ".minisign", "minisign.key")
		}
		body, err := os.ReadFile(keyFile)
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			return 1
		}
		fields := strings.Fields(string(body))
		seed, _ := hex.DecodeString(fields[1])
		signature := ed25519.Sign(ed25519.NewKeyFromSeed(seed), message)
		line := base64.StdEncoding.EncodeToString(append(append([]byte("Ed"), []byte(fields[0])...), signature...))
		sig := "untrusted comment: signature from minisign secret key\n" + line + "\ntrusted comment: " + value("-t") + "\n" +
			base64.StdEncoding.EncodeToString(make([]byte, 64)) + "\n"
		if err := os.WriteFile(value("-x"), []byte(sig), 0o644); err != nil {
			fmt.Fprintln(os.Stderr, err)
			return 1
		}
		return 0
	case has("-V"):
		sig, err := os.ReadFile(value("-x"))
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			return 1
		}
		if _, err := verifyRelease(message, sig, []string{value("-P")}); err != nil {
			fmt.Fprintln(os.Stderr, err)
			return 1
		}
		return 0
	}
	return 2
}

type signRun struct {
	dir, state, keyFile, public string
	sums                        []byte
}

// newSignRun lays out a checkout's desktop folder (the script and an install.sh carrying the key
// public), a fake gh with one draft release, and PATH for them.
func newSignRun(t *testing.T, public string) *signRun {
	t.Helper()
	r := &signRun{dir: t.TempDir(), state: t.TempDir(), public: public}
	script, err := os.ReadFile("sign-release.sh")
	if err != nil {
		t.Fatal(err)
	}
	os.WriteFile(filepath.Join(r.dir, "sign-release.sh"), script, 0o755)
	installer, _ := os.ReadFile(installerWithKey(t, public))
	os.WriteFile(filepath.Join(r.dir, "install.sh"), installer, 0o755)
	r.sums = []byte("0123abcd  daedalus-desktop-linux-amd64.tar.gz\n4567ef01  Daedalus-macOS.zip\n")
	os.WriteFile(filepath.Join(r.state, "SHA256SUMS"), r.sums, 0o644)
	os.WriteFile(filepath.Join(r.state, "assets"), []byte("daedalus-desktop-linux-amd64.tar.gz\nDaedalus-macOS.zip\nSHA256SUMS\n"), 0o644)
	os.WriteFile(filepath.Join(r.state, "draft"), []byte("true\n"), 0o644)
	return r
}

func (r *signRun) run(t *testing.T, tools string, args ...string) (string, error) {
	t.Helper()
	cmd := exec.Command("sh", append([]string{filepath.Join(r.dir, "sign-release.sh")}, args...)...)
	cmd.Env = append(os.Environ(), "PATH="+tools+string(os.PathListSeparator)+os.Getenv("PATH"), "FAKE_GH_STATE="+r.state, "HOME="+t.TempDir())
	out, err := cmd.CombinedOutput()
	return string(out), err
}

func (r *signRun) uploaded() []byte {
	body, _ := os.ReadFile(filepath.Join(r.state, "uploaded.sig"))
	return body
}

func signTools(t *testing.T, withMinisign bool) string {
	t.Helper()
	tools := t.TempDir()
	os.WriteFile(filepath.Join(tools, "gh"), []byte(fakeGH), 0o755)
	if withMinisign {
		self, err := os.Executable()
		if err != nil {
			t.Fatal(err)
		}
		os.WriteFile(filepath.Join(tools, "minisign"), []byte("#!/bin/sh\nexec env "+helperEnv+"=minisign '"+self+"' \"$@\"\n"), 0o755)
	}
	return tools
}

func TestTheReleaseIsSignedTheWayTheLauncherVerifies(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("sign-release.sh is run on the operator's Unix machine")
	}
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	public := base64.StdEncoding.EncodeToString(append(append([]byte("Ed"), []byte("key00001")...), pub...))
	keyFile := filepath.Join(t.TempDir(), "daedalus.key")
	os.WriteFile(keyFile, []byte("key00001 "+hex.EncodeToString(priv.Seed())+"\n"), 0o600)
	tools := signTools(t, true)

	r := newSignRun(t, public)
	out, err := r.run(t, tools, "desktop-v0.13.0", keyFile)
	if err != nil {
		t.Fatalf("%v\n%s", err, out)
	}
	if checked, err := verifyRelease(releaseMessage("desktop-v0.13.0", r.sums), r.uploaded(), []string{public}); !checked || err != nil {
		t.Fatalf("the uploaded SHA256SUMS.sig does not verify: %v\n%s", err, out)
	}
	if _, err := verifyRelease(releaseMessage("desktop-v0.14.0", r.sums), r.uploaded(), []string{public}); err == nil {
		t.Fatal("the signature holds for another tag")
	}
	if log, _ := os.ReadFile(filepath.Join(r.state, "log")); !strings.Contains(string(log), "release upload desktop-v0.13.0") || exists(filepath.Join(r.state, "published")) {
		t.Fatalf("uploaded or published wrongly:\n%s", log)
	}
	// --publish publishes the draft, and only after the upload.
	r = newSignRun(t, public)
	if out, err := r.run(t, tools, "desktop-v0.13.0", keyFile, "--publish"); err != nil || !exists(filepath.Join(r.state, "published")) {
		t.Fatalf("not published (%v):\n%s", err, out)
	}

	for name, c := range map[string]struct {
		prepare func(r *signRun)
		args    []string
		says    string
	}{
		"a published release": {func(r *signRun) { os.WriteFile(filepath.Join(r.state, "draft"), []byte("false\n"), 0o644) }, nil, "is not a draft"},
		"a file SHA256SUMS names is missing": {func(r *signRun) {
			os.WriteFile(filepath.Join(r.state, "assets"), []byte("SHA256SUMS\n"), 0o644)
		}, nil, "not attached"},
		"a key other than the installers'": {func(r *signRun) {
			other, _, _ := ed25519.GenerateKey(rand.Reader)
			installer, _ := os.ReadFile(installerWithKey(t, base64.StdEncoding.EncodeToString(append(append([]byte("Ed"), []byte("key00001")...), other...))))
			os.WriteFile(filepath.Join(r.dir, "install.sh"), installer, 0o755)
		}, nil, "does not verify"},
		"a tag the launcher does not call a version": {nil, []string{"desktop-v01.0.0", keyFile}, "not a desktop-vX.Y.Z tag"},
	} {
		r := newSignRun(t, public)
		if c.prepare != nil {
			c.prepare(r)
		}
		args := c.args
		if args == nil {
			args = []string{"desktop-v0.13.0", keyFile}
		}
		out, err := r.run(t, tools, args...)
		if err == nil || !strings.Contains(out, c.says) {
			t.Errorf("%s: not refused with %q (%v):\n%s", name, c.says, err, out)
		}
		if len(r.uploaded()) > 0 || exists(filepath.Join(r.state, "published")) {
			t.Errorf("%s: something was uploaded or published", name)
		}
	}
}

// The real minisign, when it is installed: a key made for the run, no password.
func TestTheReleaseIsSignedByTheRealMinisign(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("sign-release.sh is run on the operator's Unix machine")
	}
	minisign, err := exec.LookPath("minisign")
	if err != nil {
		t.Skip("minisign is not installed")
	}
	keys := t.TempDir()
	if out, err := exec.Command(minisign, "-G", "-W", "-p", filepath.Join(keys, "pub"), "-s", filepath.Join(keys, "key")).CombinedOutput(); err != nil {
		t.Fatalf("%v\n%s", err, out)
	}
	pub, _ := os.ReadFile(filepath.Join(keys, "pub"))
	public := keyLine(string(pub))
	r := newSignRun(t, public)
	out, err := r.run(t, signTools(t, false), "desktop-v0.13.0", filepath.Join(keys, "key"))
	if err != nil {
		t.Fatalf("%v\n%s", err, out)
	}
	if checked, err := verifyRelease(releaseMessage("desktop-v0.13.0", r.sums), r.uploaded(), []string{public}); !checked || err != nil {
		t.Fatalf("minisign's SHA256SUMS.sig does not verify in the launcher: %v\n%s", err, r.uploaded())
	}

	// And in install.sh, which checks it with OpenSSL.
	requireInstallerTools(t)
	installer := installerWithKey(t, public)
	first := runSigInstaller(t, installer, &sigRelease{tag: "desktop-v0.13.0"}, "")
	release := &sigRelease{tag: "desktop-v0.13.0", archive: first.release.archive, sums: first.release.sums, report: first.release.report}
	message := filepath.Join(t.TempDir(), "message")
	os.WriteFile(message, releaseMessage(release.tag, release.sums), 0o644)
	if out, err := exec.Command(minisign, "-S", "-l", "-s", filepath.Join(keys, "key"), "-m", message, "-x", message+".sig").CombinedOutput(); err != nil {
		t.Fatalf("%v\n%s", err, out)
	}
	release.sig, _ = os.ReadFile(message + ".sig")
	if c := runSigInstaller(t, installer, release, ""); c.report != "sig-present" || !strings.Contains(c.out, "signature verifies") {
		t.Fatalf("install.sh did not take minisign's signature (%q):\n%s", c.report, c.out)
	}
}
