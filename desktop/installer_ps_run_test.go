package main

// install.ps1 run for real, by PowerShell, against a release served on loopback: on Windows by
// Windows PowerShell, the one `irm | iex` runs in, and elsewhere by pwsh when it is installed (the
// script is plain PowerShell apart from $env:PROCESSOR_ARCHITECTURE, which the test sets). The
// release key is one the test makes, written where the project's key is.

import (
	"crypto/sha256"
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

type psRelease struct {
	listing []string // tags, newest listed first
	tag     string   // the one served
	archive []byte
	sums    []byte
	sig     []byte // nil: no SHA256SUMS.sig
}

func powerShell(t *testing.T) string {
	t.Helper()
	name := "pwsh"
	if runtime.GOOS == "windows" {
		name = "powershell"
	}
	path, err := exec.LookPath(name)
	if err != nil {
		t.Skip("no " + name)
	}
	// Wine ships a powershell.exe that runs nothing and exits 0; only a PowerShell that can say its
	// own version is one to run the installer with.
	out, err := exec.Command(path, "-NoProfile", "-NonInteractive", "-Command", "$PSVersionTable.PSVersion.Major").Output()
	if major := strings.TrimSpace(string(out)); err != nil || major == "" || strings.Trim(major, "0123456789") != "" {
		t.Skipf("%s does not run PowerShell (%q, %v)", path, out, err)
	}
	return path
}

// installerPSWithKey is install.ps1 with its $ReleaseKeys holding the test's key in place of the
// project's.
func installerPSWithKey(t *testing.T, key string) string {
	body, err := os.ReadFile("install.ps1")
	if err != nil {
		t.Fatal(err)
	}
	// \r too: a Windows checkout gives the script CRLF line ends, and the line was not found there.
	line := regexp.MustCompile(`\r?\n\$ReleaseKeys = @\([^)\r\n]*\)\r?\n`)
	if len(line.FindAllString(string(body), -1)) != 1 {
		t.Fatal("install.ps1 has no single $ReleaseKeys line to fill")
	}
	path := filepath.Join(t.TempDir(), "install.ps1")
	os.WriteFile(path, []byte(line.ReplaceAllLiteralString(string(body), "\n$ReleaseKeys = @('"+key+"')\n")), 0o644)
	return path
}

func runPS(t *testing.T, installer string, release *psRelease) (string, string, error) {
	t.Helper()
	const asset = "daedalus-desktop-windows-amd64.zip"
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/releases":
			var list []map[string]any
			for _, tag := range release.listing {
				list = append(list, map[string]any{"tag_name": tag, "draft": false, "prerelease": false})
			}
			json.NewEncoder(w).Encode(list)
		case "/download/" + release.tag + "/" + asset:
			w.Write(release.archive)
		case "/download/" + release.tag + "/SHA256SUMS":
			w.Write(release.sums)
		case "/download/" + release.tag + "/SHA256SUMS.sig":
			if release.sig == nil {
				http.NotFound(w, r)
				return
			}
			w.Write(release.sig)
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	target := filepath.Join(t.TempDir(), "Daedalus")
	cmd := exec.Command(powerShell(t), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", installer)
	cmd.Env = append(os.Environ(), "DAEDALUS_RELEASES_API="+server.URL, "DAEDALUS_DOWNLOAD_BASE="+server.URL+"/download", "DAEDALUS_DIR="+target)
	if os.Getenv("PROCESSOR_ARCHITECTURE") == "" {
		cmd.Env = append(cmd.Env, "PROCESSOR_ARCHITECTURE=AMD64")
	}
	out, err := cmd.CombinedOutput()
	return string(out), target, err
}

func TestTheWindowsInstallerRunsAndRefuses(t *testing.T) {
	powerShell(t)
	record, sign := testKey(t, "key00001")
	_, signOther := testKey(t, "key00001") // same id, another key: a forgery naming the right key
	installer := installerPSWithKey(t, keyLine(record))
	const asset = "daedalus-desktop-windows-amd64.zip"
	archive := makeArchive(t, asset, []archiveEntry{
		{name: "daedalus-desktop.exe", body: "the new launcher"},
		{name: "miniapp-dist", dir: true},
		{name: "miniapp-dist/index.html", body: "the app"},
	})
	sums := []byte(fmt.Sprintf("%s  %s\n", hexSum(archive), asset))
	good := func() *psRelease {
		return &psRelease{listing: []string{"desktop-v01.0.0", "desktop-v0.13.0", "desktop-v0.12.0"}, tag: "desktop-v0.13.0",
			archive: archive, sums: sums, sig: sign(releaseMessage("desktop-v0.13.0", sums))}
	}

	// A good release: the highest tag the launcher would call a version (not the one with a leading
	// zero), its key's fingerprint printed, its signature checked, its files in place.
	out, target, err := runPS(t, installer, good())
	if err != nil || !strings.Contains(out, "Installed desktop-v0.13.0") {
		t.Fatalf("a good release was not installed (%v):\n%s", err, out)
	}
	fingerprint := sha256.Sum256([]byte(keyLine(record)))
	if !strings.Contains(out, "Release key fingerprint (SHA-256 of the key line): "+hex.EncodeToString(fingerprint[:])) || !strings.Contains(out, "signature verifies") {
		t.Fatalf("the key's fingerprint or the signature check is missing:\n%s", out)
	}
	if body, _ := os.ReadFile(filepath.Join(target, "daedalus-desktop.exe")); string(body) != "the new launcher" {
		t.Fatalf("the launcher was not unpacked: %q", body)
	}

	// minisign -S -l writes a trusted comment and a second signature after the signature line.
	minisigned := good()
	minisigned.sig = append(append([]byte{}, minisigned.sig...), []byte("trusted comment: timestamp:1790778020\tfile:SHA256SUMS\n"+
		"p4XDCnJgQcU1RcCt1Ua5kwJEr5jPhiRHs3rO4G05VN0XEZDuVBv8VYOPNmWc9umAD/xnVflENZJUqzbLiDwLCw==\n")...)
	if out, _, err := runPS(t, installer, minisigned); err != nil || !strings.Contains(out, "Installed desktop-v0.13.0") {
		t.Fatalf("a signature in minisign's layout was not taken (%v):\n%s", err, out)
	}

	escaping := makeArchive(t, asset, []archiveEntry{{name: "daedalus-desktop.exe", body: "x"}, {name: "../escaped.txt", body: "x"}})
	escapingSums := []byte(fmt.Sprintf("%s  %s\n", hexSum(escaping), asset))
	for name, c := range map[string]struct {
		release *psRelease
		says    string
	}{
		"unsigned":                 {&psRelease{listing: []string{"desktop-v0.13.0"}, tag: "desktop-v0.13.0", archive: archive, sums: sums}, "not installing an unverified release"},
		"signed by another key":    {&psRelease{listing: []string{"desktop-v0.13.0"}, tag: "desktop-v0.13.0", archive: archive, sums: sums, sig: signOther(releaseMessage("desktop-v0.13.0", sums))}, "does not verify"},
		"signed for another tag":   {&psRelease{listing: []string{"desktop-v0.14.0"}, tag: "desktop-v0.14.0", archive: archive, sums: sums, sig: sign(releaseMessage("desktop-v0.13.0", sums))}, "does not verify"},
		"a broken download":        {&psRelease{listing: []string{"desktop-v0.13.0"}, tag: "desktop-v0.13.0", archive: append(append([]byte{}, archive...), 0), sums: sums, sig: sign(releaseMessage("desktop-v0.13.0", sums))}, "does not match its checksum"},
		"a path out of the folder": {&psRelease{listing: []string{"desktop-v0.13.0"}, tag: "desktop-v0.13.0", archive: escaping, sums: escapingSums, sig: sign(releaseMessage("desktop-v0.13.0", escapingSums))}, "not a plain relative path"},
	} {
		out, target, err := runPS(t, installer, c.release)
		if err == nil || !strings.Contains(out, c.says) {
			t.Errorf("%s: not refused with %q (%v):\n%s", name, c.says, err, out)
		}
		if _, err := os.Stat(filepath.Join(target, "daedalus-desktop.exe")); err == nil {
			t.Errorf("%s: the launcher was installed", name)
		}
		if _, err := os.Stat(filepath.Join(filepath.Dir(target), "escaped.txt")); err == nil {
			t.Errorf("%s: a file landed outside the folder", name)
		}
	}
}
