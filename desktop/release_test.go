package main

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

func TestOnlyAPlainReleaseTagIsAVersion(t *testing.T) {
	for tag, ok := range map[string]bool{
		"desktop-v0.12.0":        true,
		"desktop-v1.0.0":         true,
		"desktop-v1.0":           false,
		"desktop-v1.0.0-preview": false,
		"desktop-v01.0.0":        false,
		"v1.0.0":                 false,
		"dev":                    false,
		"desktop-v1.0.0.1":       false,
		"desktop-v1.-1.0":        false,
		"desktop-v0.0.0-local":   false,
		"desktop-v+1.0.0":        false,
	} {
		if got := parseVersion(tag).ok; got != ok {
			t.Errorf("%s: parsed %v, want %v", tag, got, ok)
		}
	}
	if !parseVersion("desktop-v0.13.0").newerThan(parseVersion("desktop-v0.12.9")) ||
		!parseVersion("desktop-v1.0.0").newerThan(parseVersion("desktop-v0.99.99")) ||
		parseVersion("desktop-v0.12.0").newerThan(parseVersion("desktop-v0.12.0")) {
		t.Fatal("versions compare wrong")
	}
}

func TestOnlyHTTPSOrThisMachineIsRead(t *testing.T) {
	for raw, ok := range map[string]bool{
		"https://api.github.com/repos/a/b": true,
		"http://127.0.0.1:1234/x":          true,
		"http://localhost:1234/x":          true,
		"http://[::1]:1234/x":              true,
		"http://api.github.com/repos/a/b":  false,
		"ftp://example.com/x":              false,
		"file:///etc/passwd":               false,
	} {
		if got := allowedURL(raw) == nil; got != ok {
			t.Errorf("%s: allowed %v, want %v", raw, got, ok)
		}
	}
}

// releaseServer serves a GitHub-shaped release listing from this machine.
func releaseServer(t *testing.T, releases []map[string]any) *httptest.Server {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/releases" {
			http.NotFound(w, r)
			return
		}
		_ = json.NewEncoder(w).Encode(releases)
	}))
	t.Cleanup(server.Close)
	t.Setenv("DAEDALUS_RELEASES_API", server.URL)
	return server
}

func withVersion(t *testing.T, v string) {
	t.Helper()
	old := version
	version = v
	t.Cleanup(func() { version = old })
}

func assetsFor(names ...string) []map[string]any {
	var out []map[string]any
	for _, name := range names {
		out = append(out, map[string]any{"name": name, "browser_download_url": "http://127.0.0.1:1/" + name})
	}
	return out
}

func TestTheNewestCompleteReleaseIsOffered(t *testing.T) {
	asset, err := platformAsset(runtime.GOOS, runtime.GOARCH)
	if err != nil {
		t.Skip(err)
	}
	releaseServer(t, []map[string]any{
		// Newest by version but still uploading: no archive yet, so not offered.
		{"tag_name": "desktop-v0.15.0", "assets": assetsFor("SHA256SUMS")},
		{"tag_name": "desktop-v0.16.0-rc1", "assets": assetsFor(asset, "SHA256SUMS", signatureAsset)},
		{"tag_name": "desktop-v0.17.0", "prerelease": true, "assets": assetsFor(asset, "SHA256SUMS", signatureAsset)},
		{"tag_name": "desktop-v0.18.0", "draft": true, "assets": assetsFor(asset, "SHA256SUMS", signatureAsset)},
		{"tag_name": "desktop-v0.13.0", "assets": assetsFor(asset, "SHA256SUMS", signatureAsset)},
		{"tag_name": "desktop-v0.14.0", "assets": assetsFor(asset, "SHA256SUMS", signatureAsset)},
		{"tag_name": "something-else-v9.0.0", "assets": assetsFor(asset, "SHA256SUMS", signatureAsset)},
	})
	offer, _, err := FindUpgrade(context.Background(), "desktop-v0.12.0")
	if err != nil {
		t.Fatal(err)
	}
	if offer == nil || offer.To != "desktop-v0.14.0" || offer.Asset != asset {
		t.Fatalf("offer = %+v, want desktop-v0.14.0", offer)
	}
	offer, _, err = FindUpgrade(context.Background(), "desktop-v0.14.0")
	if err != nil || offer != nil {
		t.Fatalf("the newest release was offered an upgrade: %+v %v", offer, err)
	}
}

func TestADevBuildIsNotOfferedAnything(t *testing.T) {
	if _, _, err := FindUpgrade(context.Background(), "dev"); err == nil {
		t.Fatal("a dev build was compared with releases")
	}
}

func TestAReleaseIsAnnouncedOnceItWasShown(t *testing.T) {
	p, _ := NewPaths(t.TempDir())
	if notified(p, "desktop-v0.13.0") {
		t.Fatal("announced before it was")
	}
	markNotified(p, "desktop-v0.13.0")
	if !notified(p, "desktop-v0.13.0") || notified(p, "desktop-v0.14.0") {
		t.Fatal("the record is wrong")
	}
}

func TestTheUpgradeCommandIsRunnableAsWritten(t *testing.T) {
	for _, c := range []struct{ goos, exe, data, want string }{
		{"linux", "/home/a b/Daedalus/daedalus-desktop", "/home/a b/Daedalus/data", `'/home/a b/Daedalus/daedalus-desktop' upgrade --data '/home/a b/Daedalus/data'`},
		{"darwin", "/Users/o'k/Daedalus.app/Contents/MacOS/daedalus-desktop", "/Users/o'k/data", `'/Users/o'\''k/Daedalus.app/Contents/MacOS/daedalus-desktop' upgrade --data '/Users/o'\''k/data'`},
		{"windows", `C:\Users\o'k\Daedalus\daedalus-desktop.exe`, `C:\Users\o'k\Daedalus\data`, `& 'C:\Users\o''k\Daedalus\daedalus-desktop.exe' upgrade --data 'C:\Users\o''k\Daedalus\data'`},
	} {
		if got := commandLineFor(c.goos, c.exe, c.data); got != c.want {
			t.Errorf("%s: %s", c.goos, got)
		}
	}
	if runtime.GOOS != "windows" {
		// And sh runs it: the quoting survives a real shell.
		line := commandLineFor("linux", "/bin/echo", "/tmp/x y")
		out, err := exec.Command("sh", "-c", line).Output()
		if err != nil || strings.TrimSpace(string(out)) != "upgrade --data /tmp/x y" {
			t.Fatalf("sh ran %q as %q, %v", line, out, err)
		}
	}
}

func TestTheNotificationAsksAndDoesNotInstall(t *testing.T) {
	n := UpgradeNotification(LangEN, Offer{From: "desktop-v0.12.0", To: "desktop-v0.13.0"})
	if n.Title != "Daedalus 0.13.0 is available" || n.Link != "" {
		t.Fatalf("notification = %+v", n)
	}
	ru := UpgradeNotification(LangRU, Offer{From: "desktop-v0.12.0", To: "desktop-v0.13.0"})
	if ru.Title == n.Title {
		t.Fatal("the Russian notification is the English one")
	}
}

func TestTheCheckCanBeTurnedOff(t *testing.T) {
	t.Setenv("DAEDALUS_UPDATE_CHECK", "off")
	if !updateCheckOff() {
		t.Fatal("DAEDALUS_UPDATE_CHECK=off did not turn it off")
	}
}

func TestChecksumLines(t *testing.T) {
	list := "aaaa  other.zip\n" +
		"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef  daedalus-desktop-linux-amd64.tar.gz\n"
	if got := checksumFor(list, "daedalus-desktop-linux-amd64.tar.gz"); got != "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef" {
		t.Fatalf("got %q", got)
	}
	if checksumFor(list, "other.zip") != "" || checksumFor(list, "missing") != "" {
		t.Fatal("a malformed or missing line was read as a checksum")
	}
}

func TestARunningLauncherAnnouncesANewReleaseOnce(t *testing.T) {
	asset, err := platformAsset(runtime.GOOS, runtime.GOARCH)
	if err != nil {
		t.Skip(err)
	}
	withVersion(t, "desktop-v0.12.0")
	releaseServer(t, []map[string]any{{"tag_name": "desktop-v0.13.0", "assets": assetsFor(asset, "SHA256SUMS", signatureAsset)}})
	delay, interval := releaseCheckDelay, releaseCheckInterval
	releaseCheckDelay, releaseCheckInterval = time.Millisecond, time.Millisecond
	t.Cleanup(func() { releaseCheckDelay, releaseCheckInterval = delay, interval })
	p, _ := NewPaths(t.TempDir())
	app := NewApp(p)
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	var shown []Notification
	done := make(chan struct{})
	go func() {
		app.CheckReleases(ctx, func(n Notification) error {
			// The first showing fails, as on a machine whose notifications are off for a moment; it is
			// tried again at the next check, and then recorded.
			shown = append(shown, n)
			if len(shown) == 1 {
				return errors.New("no notification daemon")
			}
			return nil
		})
		close(done)
	}()
	// Let it check several times, then stop it.
	for app.Status(ctx).Upgrade == nil && ctx.Err() == nil {
		time.Sleep(5 * time.Millisecond)
	}
	time.Sleep(50 * time.Millisecond)
	cancel()
	<-done
	if len(shown) != 2 || shown[1].Title != "Daedalus 0.13.0 is available" || !strings.Contains(shown[1].Body, "upgrade --data") {
		t.Fatalf("shown %+v", shown)
	}
	if offer := app.Status(context.Background()).Upgrade; offer == nil || offer.To != "desktop-v0.13.0" {
		t.Fatalf("the status does not carry the offer: %+v", offer)
	}
	if _, err := os.Stat(filepath.Join(p.Data, "state")); err == nil {
		t.Fatal("the check wrote more than its own note")
	}
}

// The app's "check for updates" button and the periodic check are one call, and the status the app
// reads afterwards says what it found, when, and this launcher's own version.
func TestACheckOnRequestIsWhatTheStatusSays(t *testing.T) {
	asset, err := platformAsset(runtime.GOOS, runtime.GOARCH)
	if err != nil {
		t.Skip(err)
	}
	t.Setenv("DAEDALUS_UPDATE_CHECK", "")
	saved := version
	version = "desktop-v0.12.0"
	t.Cleanup(func() { version = saved })
	releaseServer(t, []map[string]any{{"tag_name": "desktop-v0.14.0", "assets": assetsFor(asset, "SHA256SUMS", signatureAsset)}})
	p, _ := NewPaths(t.TempDir())
	app := NewApp(p)
	if got := app.Status(context.Background()); got.UpgradeChecked != "" || got.Upgrade != nil || got.Version != "desktop-v0.12.0" {
		t.Fatalf("before any check the status reads %+v", got)
	}
	offer, err := app.CheckUpgrade(context.Background())
	if err != nil || offer == nil || offer.To != "desktop-v0.14.0" {
		t.Fatalf("offer %+v, err %v", offer, err)
	}
	got := app.Status(context.Background())
	if got.Upgrade == nil || got.Upgrade.To != "desktop-v0.14.0" || got.UpgradeChecked == "" || got.UpgradeError != "" {
		t.Fatalf("after the check the status reads %+v", got)
	}

	version = "dev"
	if _, err := app.CheckUpgrade(context.Background()); err == nil {
		t.Fatal("a dev build checked releases")
	}
	if got := app.Status(context.Background()); got.UpgradeError == "" {
		t.Fatal("a check that could not run left no reason in the status")
	}
}
