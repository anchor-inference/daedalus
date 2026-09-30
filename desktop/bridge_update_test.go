package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

// The bridge: an installation whose launcher predates `upgrade` (v0.12.0), upgraded by the new
// launcher an installer downloaded. The installer's two files are written here; the new launcher
// is this test process running under the new version.

type bridgeCase struct {
	in      *installation
	u       *Upgrader
	archive string
	sums    string
}

func newBridge(t *testing.T, release *fixtureRelease) *bridgeCase {
	t.Helper()
	withVersion(t, "desktop-v0.13.0")
	in := newInstallation(t)
	asset, err := platformAsset(runtime.GOOS, runtime.GOARCH)
	if err != nil {
		t.Skip(err)
	}
	downloads := t.TempDir()
	archive := makeArchive(t, asset, release.entries)
	archivePath := filepath.Join(downloads, asset)
	os.WriteFile(archivePath, archive, 0o644)
	sumsPath := filepath.Join(downloads, "SHA256SUMS")
	sum := fmt.Sprintf("%x", sha256.Sum256(archive))
	if release.badSum {
		sum = strings.Repeat("0", 64)
	}
	os.WriteFile(sumsPath, []byte(sum+"  "+asset+"\n"), 0o644)
	// The bridge's own file: the launcher out of the same archive, as install.sh unpacks it.
	self := filepath.Join(t.TempDir(), "daedalus-desktop")
	os.WriteFile(self, []byte("new launcher"), 0o755)

	u := in.upgrader("yes\n", true)
	u.opts.bridge, u.opts.bridgeRoot, u.opts.bridgeArchive, u.opts.bridgeSums = true, in.root, archivePath, sumsPath
	u.opts.self = self
	u.opts.executable = self
	u.opts.probeVersion = func(_ context.Context, exe string) (string, error) {
		if body, _ := os.ReadFile(exe); string(body) != "old launcher" {
			return "", fmt.Errorf("%s is not the installed launcher", exe)
		}
		return "desktop-v0.12.0", nil
	}
	// The new launcher in place runs --finish; this process already is that version.
	u.opts.runNewBinary = func(ctx context.Context, exe string, args []string) error {
		if body, _ := os.ReadFile(exe); string(body) != "new launcher" {
			return fmt.Errorf("%s is %q, not the new launcher", exe, body)
		}
		next := &Upgrader{paths: in.paths, stack: in.stack, mode: ModeNative, out: u.out, opts: upgradeOptions{finish: true, now: u.opts.now}}
		return next.Run(ctx)
	}
	return &bridgeCase{in: in, u: u, archive: archivePath, sums: sumsPath}
}

func (b *bridgeCase) unchanged(t *testing.T) {
	t.Helper()
	in := b.in
	if in.file(t, launcherName()) != "old launcher" || in.file(t, "ptyd") != "old ptyd" || in.file(t, "data/state/daedalus.sqlite") != "schema v1\n" {
		t.Fatalf("something changed:\n%s", b.u.out)
	}
	if in.stack.updates != 0 {
		t.Fatal("the stack was updated")
	}
	if err := InterruptedUpgrade(in.paths); err != nil {
		t.Fatalf("a refusal left the launcher blocked: %v", err)
	}
}

func TestAnOldInstallationIsBridgedWithABackupOfDataAndLauncher(t *testing.T) {
	b := newBridge(t, newRelease())
	if err := b.u.Run(context.Background()); err != nil {
		t.Fatalf("%v\n%s", err, b.u.out)
	}
	in := b.in
	if in.file(t, launcherName()) != "new launcher" || in.file(t, "data/state/daedalus.sqlite") != "schema v2\n" {
		t.Fatal("not upgraded")
	}
	journal, _ := readJournal(in.paths)
	if journal.Stage != stageCommitted || journal.From != "desktop-v0.12.0" || journal.To != "desktop-v0.13.0" {
		t.Fatalf("journal %+v", journal)
	}
	manifest, err := VerifyBackup(journal.Backup)
	if err != nil {
		t.Fatal(err)
	}
	// The old launcher is in the backup, checked, with the bytes it had.
	var launcherFile *BackupEntry
	for i, entry := range manifest.LauncherEntries {
		if entry.Path == launcherName() {
			launcherFile = &manifest.LauncherEntries[i]
		}
	}
	if launcherFile == nil || launcherFile.SHA256 != fmt.Sprintf("%x", sha256.Sum256([]byte("old launcher"))) {
		t.Fatalf("the old launcher is not in the backup: %+v", manifest.LauncherEntries)
	}
	if manifest.LauncherRoot != in.root {
		t.Fatalf("launcher root %q", manifest.LauncherRoot)
	}
}

func TestABridgedUpgradeThatFailsPutsBothBack(t *testing.T) {
	b := newBridge(t, newRelease())
	b.in.stack.fail = true
	if err := b.u.Run(context.Background()); err == nil {
		t.Fatal("reported success")
	}
	in := b.in
	if in.file(t, launcherName()) != "old launcher" || in.file(t, "ptyd") != "old ptyd" || in.file(t, "data/state/daedalus.sqlite") != "schema v1\n" {
		t.Fatalf("not put back:\n%s", b.u.out)
	}
	if exists(filepath.Join(in.paths.State, "added-by-migration")) {
		t.Fatal("the migration's file survived")
	}
	if journal, _ := readJournal(in.paths); journal.Stage != stageRolledBack {
		t.Fatalf("stage %s", journal.Stage)
	}
}

func TestTheLauncherComesBackFromTheBackupWhenItsKeptCopyIsGone(t *testing.T) {
	b := newBridge(t, newRelease())
	b.in.stack.fail = true
	finish := b.u.opts.runNewBinary
	b.u.opts.runNewBinary = func(ctx context.Context, exe string, args []string) error {
		journal, _ := readJournal(b.in.paths)
		// Someone tidied up: the renamed copies of the old launcher are gone.
		os.RemoveAll(filepath.Join(journal.Work, "old"))
		return finish(ctx, exe, args)
	}
	if err := b.u.Run(context.Background()); err == nil {
		t.Fatal("reported success")
	}
	if b.in.file(t, launcherName()) != "old launcher" || b.in.file(t, "ptyd") != "old ptyd" {
		t.Fatalf("the launcher did not come back from the backup:\n%s", b.u.out)
	}
	// miniapp-dist was replaced too and comes back the same way.
	if b.in.file(t, "miniapp-dist/index.html") != "old app" {
		t.Fatal("miniapp-dist did not come back")
	}
}

func TestABridgeRunByTheWrongBinaryChangesNothing(t *testing.T) {
	b := newBridge(t, newRelease())
	os.WriteFile(b.u.opts.self, []byte("some other launcher"), 0o755)
	if err := b.u.Run(context.Background()); err == nil || !strings.Contains(err.Error(), "not the one in") {
		t.Fatalf("err = %v", err)
	}
	b.unchanged(t)
	if exists(backupsDir(b.in.paths)) {
		t.Fatal("a backup was taken")
	}
}

func TestABridgeOverANewerOrUnknownLauncherChangesNothing(t *testing.T) {
	for _, installed := range []string{"desktop-v0.13.0", "desktop-v0.14.0", "dev", ""} {
		b := newBridge(t, newRelease())
		b.u.opts.probeVersion = func(context.Context, string) (string, error) { return installed, nil }
		if err := b.u.Run(context.Background()); err == nil {
			t.Fatalf("%q: bridged", installed)
		}
		b.unchanged(t)
	}
	b := newBridge(t, newRelease())
	b.u.opts.probeVersion = func(context.Context, string) (string, error) { return "", errors.New("exec format error") }
	if err := b.u.Run(context.Background()); err == nil {
		t.Fatal("bridged a launcher that did not run")
	}
	b.unchanged(t)
}

func TestABridgeWithABadChecksumChangesNothing(t *testing.T) {
	release := newRelease()
	release.badSum = true
	b := newBridge(t, release)
	if err := b.u.Run(context.Background()); err == nil || !strings.Contains(err.Error(), "checksum") {
		t.Fatalf("err = %v", err)
	}
	b.unchanged(t)
}

func TestABridgeInDockerModeChangesNothing(t *testing.T) {
	b := newBridge(t, newRelease())
	b.u.mode = ModeDocker
	if err := b.u.Run(context.Background()); !errors.Is(err, errDockerNotCovered) {
		t.Fatalf("err = %v", err)
	}
	b.unchanged(t)
	if exists(backupsDir(b.in.paths)) {
		t.Fatal("a backup was taken")
	}
}

func TestABridgeWithoutAYesChangesNothing(t *testing.T) {
	b := newBridge(t, newRelease())
	b.u.opts.stdin, b.u.opts.stdinIsTTY = strings.NewReader(""), true
	if err := b.u.Run(context.Background()); err == nil {
		t.Fatal("bridged without a yes")
	}
	b.unchanged(t)
}

// A backup that cannot be written or does not verify stops everything before a single file is
// replaced — for the bridge and for a launcher's own upgrade alike.
func TestAFailedBackupStopsBeforeAnythingIsReplaced(t *testing.T) {
	for _, bridged := range []bool{true, false} {
		for _, how := range []string{"damaged", "unwritable"} {
			var u *Upgrader
			var in *installation
			if bridged {
				b := newBridge(t, newRelease())
				u, in = b.u, b.in
			} else {
				withVersion(t, "desktop-v0.12.0")
				in = newInstallation(t)
				newRelease().serve(t, "desktop-v0.13.0")
				u = in.upgrader("yes\n", true)
			}
			switch how {
			case "damaged":
				u.opts.damageBackup = func(dir string) { os.WriteFile(filepath.Join(dir, backupArchive), []byte("damaged"), 0o600) }
			case "unwritable":
				if runtime.GOOS == "windows" || os.Getuid() == 0 {
					continue
				}
				os.MkdirAll(backupsDir(in.paths), 0o500)
				t.Cleanup(func() { os.Chmod(backupsDir(in.paths), 0o700) })
			}
			err := u.Run(context.Background())
			if err == nil || !strings.Contains(err.Error(), "nothing was changed") {
				t.Fatalf("bridge=%v %s: err = %v", bridged, how, err)
			}
			if in.file(t, launcherName()) != "old launcher" || in.file(t, "data/state/daedalus.sqlite") != "schema v1\n" || in.stack.updates != 0 {
				t.Fatalf("bridge=%v %s: something changed", bridged, how)
			}
			if err := InterruptedUpgrade(in.paths); err != nil {
				t.Fatalf("bridge=%v %s: blocked after a refusal: %v", bridged, how, err)
			}
			// Nothing of the attempt is left: no staging folder, no journal, no unverified backup.
			if entries, _ := os.ReadDir(backupsDir(in.paths)); len(entries) != 0 {
				t.Fatalf("bridge=%v %s: a backup that did not verify was kept", bridged, how)
			}
			if exists(journalFile(in.paths)) || exists(filepath.Join(in.root, ".daedalus-upgrade")) {
				t.Fatalf("bridge=%v %s: the attempt left files behind", bridged, how)
			}
		}
	}
}

// ---- update ------------------------------------------------------------------------------------

func updater(t *testing.T) (*Upgrader, *installation) {
	t.Helper()
	withVersion(t, "desktop-v0.13.0")
	in := newInstallation(t)
	u := &Upgrader{paths: in.paths, stack: in.stack, mode: ModeNative, out: &bytes.Buffer{}, opts: upgradeOptions{yes: true,
		now: func() time.Time { return time.Unix(1_800_000_000, 0) }}}
	return u, in
}

func TestAnUpdateBacksUpBeforeItMovesTheStack(t *testing.T) {
	u, in := updater(t)
	if err := u.update(context.Background()); err != nil {
		t.Fatalf("%v\n%s", err, u.out)
	}
	if in.file(t, "data/state/daedalus.sqlite") != "schema v2\n" || in.stack.updates != 1 {
		t.Fatal("not updated")
	}
	journal, _ := readJournal(in.paths)
	if journal.Kind != kindUpdate || journal.Stage != stageCommitted {
		t.Fatalf("journal %+v", journal)
	}
	manifest, err := VerifyBackup(journal.Backup)
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range manifest.Entries {
		if entry.Path == "state/daedalus.sqlite" && entry.SHA256 != fmt.Sprintf("%x", sha256.Sum256([]byte("schema v1\n"))) {
			t.Fatal("the backup is not of the data before the update")
		}
	}
	if in.file(t, launcherName()) != "old launcher" {
		t.Fatal("an update touched the launcher")
	}
}

func TestAFailedUpdatePutsTheDataBack(t *testing.T) {
	u, in := updater(t)
	in.stack.fail = true
	if err := u.update(context.Background()); err == nil {
		t.Fatal("reported success")
	}
	if in.file(t, "data/state/daedalus.sqlite") != "schema v1\n" || exists(filepath.Join(in.paths.State, "added-by-migration")) {
		t.Fatal("not put back")
	}
	if journal, _ := readJournal(in.paths); journal.Stage != stageRolledBack || journal.Kind != kindUpdate {
		t.Fatalf("journal %+v", journal)
	}
	if err := InterruptedUpgrade(in.paths); err != nil {
		t.Fatal(err)
	}
}

func TestAnUpdateWhoseBackupFailsDoesNotMoveTheStack(t *testing.T) {
	u, in := updater(t)
	u.opts.damageBackup = func(dir string) { os.WriteFile(filepath.Join(dir, backupArchive), []byte("damaged"), 0o600) }
	if err := u.update(context.Background()); err == nil || !strings.Contains(err.Error(), "nothing was changed") {
		t.Fatalf("err = %v", err)
	}
	if in.stack.updates != 0 || in.file(t, "data/state/daedalus.sqlite") != "schema v1\n" {
		t.Fatal("the stack moved without a verified backup")
	}
}

func TestAnUpdateCutOffIsFoundAndRolledBack(t *testing.T) {
	u, in := updater(t)
	u.stack = &cutAfterUpdate{fakeStack: in.stack}
	func() {
		defer func() { recover() }()
		u.update(context.Background())
	}()
	if err := InterruptedUpgrade(in.paths); err == nil || !strings.Contains(err.Error(), "update of the checkouts did not finish") {
		t.Fatalf("not blocked: %v", err)
	}
	app := NewApp(in.paths)
	app.SetMode(ModeNative)
	if err := app.Update(context.Background()); err == nil || !strings.Contains(err.Error(), "did not finish") {
		t.Fatal("a second update ran on top of an unfinished one")
	}
	rollback := &Upgrader{paths: in.paths, stack: in.stack, mode: ModeNative, out: &bytes.Buffer{}, opts: upgradeOptions{rollback: true}}
	if err := rollback.Run(context.Background()); err == nil || !strings.Contains(err.Error(), "rolled back") {
		t.Fatalf("err = %v", err)
	}
	if in.file(t, "data/state/daedalus.sqlite") != "schema v1\n" {
		t.Fatal("not put back")
	}
}

// cutAfterUpdate is a stack whose machine loses power right after the migration ran.
type cutAfterUpdate struct{ *fakeStack }

func (c *cutAfterUpdate) UpdateAndCheck(ctx context.Context) error {
	c.fakeStack.UpdateAndCheck(ctx)
	panic("power cut")
}

func TestDockerUpdateIsRefusedWithNothingChanged(t *testing.T) {
	u, in := updater(t)
	u.mode = ModeDocker
	if err := u.update(context.Background()); !errors.Is(err, errDockerNotCovered) {
		t.Fatalf("err = %v", err)
	}
	if in.stack.stops != 0 || in.stack.updates != 0 || exists(backupsDir(in.paths)) {
		t.Fatal("something was done")
	}
	// The page's button and the command line go through the same place.
	app := NewApp(in.paths)
	app.SetMode(ModeDocker)
	if err := app.Update(context.Background()); !errors.Is(err, errDockerNotCovered) {
		t.Fatalf("App.Update in Docker mode: %v", err)
	}
	if !strings.Contains(errDockerNotCovered.Error(), "UPDATES.md") {
		t.Fatal("the refusal does not say where the manual path is")
	}
}

// ---- the notice on the launcher's page ---------------------------------------------------------

func TestTheStatusPageHasTheUpgradeCard(t *testing.T) {
	body, err := uiFiles.ReadFile("ui/status.html")
	if err != nil {
		t.Fatal(err)
	}
	for _, id := range []string{`id="upgrade"`, `id="upgrade-title"`, `id="upgrade-body"`} {
		if !strings.Contains(string(body), id) {
			t.Fatalf("status.html has no %s", id)
		}
	}
	for _, lang := range []Lang{LangEN, LangRU} {
		for _, key := range []string{"upgrade.card.title", "upgrade.card.body"} {
			if Translate(lang, key) == key || !strings.Contains(Translate(lang, key), "%s") {
				t.Fatalf("%s: %s is missing or has no place for the version", lang, key)
			}
		}
	}
}

// TestTheUpgradeCardIsShownInABrowser renders the real status page in a headless Chromium (through
// Python's Playwright) and reads the card back out of the page its own script built. It runs only
// with DAEDALUS_BROWSER_TEST=1, because it starts a browser.
func TestTheUpgradeCardIsShownInABrowser(t *testing.T) {
	if os.Getenv("DAEDALUS_BROWSER_TEST") != "1" {
		t.Skip("set DAEDALUS_BROWSER_TEST=1 to render the page in a headless Chromium")
	}
	p, _ := NewPaths(filepath.Join(t.TempDir(), "data"))
	p.EnsureDirs()
	os.WriteFile(p.Env, []byte("X=1\n"), 0o600)
	os.WriteFile(p.Mode, []byte("native\n"), 0o600)
	app := NewApp(p)
	app.SetMode(ModeNative)
	app.offer = &Offer{From: "desktop-v0.12.0", To: "desktop-v0.13.0"}
	server := NewServer(app, 0)
	if err := server.Start(); err != nil {
		t.Fatal(err)
	}
	defer server.Stop(context.Background())
	shot := os.Getenv("DAEDALUS_BROWSER_SHOT")
	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, "python3", "-c", browserScript, server.URL()+"status", shot).CombinedOutput()
	if err != nil {
		t.Fatalf("%v\n%s", err, out)
	}
	got := string(out)
	if !strings.Contains(got, "visible=True") || !strings.Contains(got, "Daedalus 0.13.0 is available") || !strings.Contains(got, "This launcher is 0.12.0") {
		t.Fatalf("the card is not shown as it should be:\n%s", got)
	}
}

const browserScript = `
import sys
from playwright.sync_api import sync_playwright
url, shot = sys.argv[1], sys.argv[2]
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1000, "height": 1100})
    page.goto(url)
    page.wait_for_selector("#upgrade:not([hidden])", timeout=15000)
    # The cards fade in; visible means drawn and opaque, not only present in the layout.
    page.wait_for_function("getComputedStyle(document.getElementById('upgrade')).opacity === '1'", timeout=15000)
    card = page.locator("#upgrade")
    print("visible=%s" % card.is_visible())
    print(card.inner_text())
    if shot:
        page.screenshot(path=shot, full_page=True)
    browser.close()
`
