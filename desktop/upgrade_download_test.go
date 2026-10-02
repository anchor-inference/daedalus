package main

import (
	"context"
	"os"
	"strings"
	"testing"
	"time"
)

func waitForDownload(t *testing.T, app *App) Download {
	t.Helper()
	deadline := time.Now().Add(20 * time.Second)
	for time.Now().Before(deadline) {
		if state := app.downloadState(); state.State != "running" {
			return state
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatal("the download never finished")
	return Download{}
}

// The app's update button downloads and checks the release first, while everything still runs; the
// upgrade it then starts installs those files and does not fetch the archive again.
func TestAReleaseDownloadedFromTheAppIsWhatTheUpgradeInstalls(t *testing.T) {
	withVersion(t, "desktop-v0.12.0")
	in := newInstallation(t)
	release := newRelease()
	release.serve(t, "desktop-v0.13.0")
	app := NewApp(in.paths)
	offer, err := app.CheckUpgrade(context.Background())
	if err != nil || offer == nil {
		t.Fatalf("offer %+v, err %v", offer, err)
	}
	if offer.Package != "" {
		t.Skipf("this test binary counts as a package-managed copy (%s)", offer.Package)
	}
	if state := app.downloadState(); state.Ready || state.State != "idle" {
		t.Fatalf("before any download the state is %+v", state)
	}
	if err := app.StartDownload(context.Background()); err != nil {
		t.Fatal(err)
	}
	if state := waitForDownload(t, app); state.State != "done" || !state.Ready || state.Done == 0 {
		t.Fatalf("after the download the state is %+v", state)
	}
	if got := app.Status(context.Background()); !got.Download.Ready {
		t.Fatalf("the status does not say the download is ready: %+v", got.Download)
	}

	before := release.requests
	u := in.upgrader("yes\n", true)
	if err := u.Run(context.Background()); err != nil {
		t.Fatalf("%v\n%s", err, u.out)
	}
	if in.file(t, launcherName()) != "new launcher" {
		t.Fatal("the downloaded release was not installed")
	}
	// The release list is read again (it names the offer); nothing else is fetched.
	if fetched := release.requests - before; fetched != 1 {
		t.Fatalf("the upgrade made %d requests; with the release downloaded it needs only the release list", fetched)
	}
	if !strings.Contains(u.out.(interface{ String() string }).String(), "downloaded beforehand") {
		t.Fatalf("the upgrade did not say it used the downloaded release:\n%s", u.out)
	}
}

// A download that does not match SHA256SUMS fails while the app is open, and is never ready.
func TestABrokenDownloadIsRefusedBeforeAnythingCloses(t *testing.T) {
	withVersion(t, "desktop-v0.12.0")
	in := newInstallation(t)
	release := newRelease()
	release.badSum = true
	release.serve(t, "desktop-v0.13.0")
	app := NewApp(in.paths)
	offer, err := app.CheckUpgrade(context.Background())
	if err != nil || offer == nil {
		t.Fatalf("offer %+v, err %v", offer, err)
	}
	if offer.Package != "" {
		t.Skip("package-managed test binary")
	}
	if err := app.StartDownload(context.Background()); err != nil {
		t.Fatal(err)
	}
	state := waitForDownload(t, app)
	if state.State != "failed" || state.Ready || !strings.Contains(state.Error, "checksum") {
		t.Fatalf("a download with the wrong checksum ended as %+v", state)
	}
	if stagedSource(in.paths, offer) != nil {
		t.Fatal("a refused download is offered to the upgrade")
	}
}

// The upgrade trusts what is on disk only as far as the checksum recorded when it was checked; a
// file changed since then is refused rather than installed.
func TestADownloadChangedOnDiskIsNotInstalled(t *testing.T) {
	withVersion(t, "desktop-v0.12.0")
	in := newInstallation(t)
	newRelease().serve(t, "desktop-v0.13.0")
	app := NewApp(in.paths)
	offer, err := app.CheckUpgrade(context.Background())
	if err != nil || offer == nil {
		t.Fatalf("offer %+v, err %v", offer, err)
	}
	if offer.Package != "" {
		t.Skip("package-managed test binary")
	}
	if err := app.StartDownload(context.Background()); err != nil {
		t.Fatal(err)
	}
	if state := waitForDownload(t, app); !state.Ready {
		t.Fatalf("not ready: %+v", state)
	}
	if err := os.WriteFile(stagedFiles(in.paths, offer.To, offer.Asset).archive, []byte("something else"), 0o600); err != nil {
		t.Fatal(err)
	}
	source := stagedSource(in.paths, offer)
	if source == nil {
		t.Fatal("the staged files vanished")
	}
	if _, _, err := source.fetch(context.Background()); err == nil {
		t.Fatal("an archive changed after its check was handed to the upgrade")
	}
}
