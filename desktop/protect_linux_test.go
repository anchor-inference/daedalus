//go:build linux

package main

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// These are the update and the upgrade with the writer fence as their protection, on ext4. The
// other tests of the two commands run them with the verified backup, which is what Windows and
// macOS use (TestMain sets DAEDALUS_DATA_FENCE=off unless a test asks otherwise).

func fencedInstallation(t *testing.T) {
	t.Helper()
	fenceRequireExt4(t, os.TempDir())
	t.Setenv("DAEDALUS_DATA_FENCE", "auto")
}

func TestAnUpdateSwitchesToAFencedCopyAndKeepsTheDataFromBefore(t *testing.T) {
	fencedInstallation(t)
	u, in := updater(t)
	original := fenceIno(t, in.paths.Data)
	if err := u.update(context.Background()); err != nil {
		t.Fatalf("%v\n%s", err, u.out)
	}
	journal, _ := readJournal(in.paths)
	if journal.Stage != stageCommitted || journal.Pre == "" || journal.Backup != "" {
		t.Fatalf("journal %+v\n%s", journal, u.out)
	}
	if in.file(t, "data/state/daedalus.sqlite") != "schema v2\n" {
		t.Fatal("the update did not reach the live data")
	}
	pre := filepath.Join(fenceControlPath(in.paths.Data), "retained", journal.Pre)
	if fenceIno(t, pre) != original || fenceRead(t, filepath.Join(pre, "state", "daedalus.sqlite")) != "schema v1\n" {
		t.Fatal("the data from before the update is not kept whole")
	}
	if exists(in.paths.LegacyRuntime) {
		t.Fatal("the runtime is still inside the data folder")
	}
	if exists(backupsDir(in.paths)) {
		t.Fatal("a backup was taken although the fence protected the data")
	}
}

func TestAFencedUpdateThatDoesNotComeUpPutsTheDataFromBeforeBack(t *testing.T) {
	fencedInstallation(t)
	u, in := updater(t)
	original := fenceIno(t, in.paths.Data)
	in.stack.fail = true
	if err := u.update(context.Background()); err == nil {
		t.Fatalf("reported success\n%s", u.out)
	}
	if fenceIno(t, in.paths.Data) != original || in.file(t, "data/state/daedalus.sqlite") != "schema v1\n" || exists(filepath.Join(in.paths.State, "added-by-migration")) {
		t.Fatalf("the data from before is not live again\n%s", u.out)
	}
	failed, _ := filepath.Glob(filepath.Join(fenceControlPath(in.paths.Data), "retained", "failed-*"))
	var trees []string
	for _, path := range failed {
		if !strings.HasSuffix(path, ".json") {
			trees = append(trees, path)
		}
	}
	if len(trees) != 1 || fenceRead(t, filepath.Join(trees[0], "state", "daedalus.sqlite")) != "schema v2\n" {
		t.Fatalf("what the failed version left is not kept: %v", failed)
	}
	if journal, _ := readJournal(in.paths); journal.Stage != stageRolledBack {
		t.Fatalf("journal %+v", journal)
	}
	if err := InterruptedUpgrade(in.paths); err != nil {
		t.Fatal(err)
	}
	// And the next update works.
	in.stack.fail = false
	if err := u.update(context.Background()); err != nil {
		t.Fatalf("the update after a rollback: %v\n%s", err, u.out)
	}
}

func TestAFencedUpdateRefusedByAWriterChangesNothing(t *testing.T) {
	fencedInstallation(t)
	u, in := updater(t)
	child := startFenceChild(t, "fence-hold-open", "HELPER_PATH="+filepath.Join(in.paths.State, "daedalus.sqlite"))
	child.expect(t, "ready", 5*time.Second)
	original := fenceIno(t, in.paths.Data)
	err := u.update(context.Background())
	if err == nil || !strings.Contains(err.Error(), "nothing was changed") || !strings.Contains(err.Error(), "state/daedalus.sqlite") {
		t.Fatalf("err = %v\n%s", err, u.out)
	}
	if in.stack.updates != 0 || fenceIno(t, in.paths.Data) != original {
		t.Fatal("the stack moved or the data was switched although a writer held it")
	}
	if _, err := readJournal(in.paths); err == nil {
		t.Fatal("a refused update left a journal")
	}
}

func TestAFencedUpgradeKeepsTheMigratedDataWhenItWorks(t *testing.T) {
	fencedInstallation(t)
	withVersion(t, "desktop-v0.12.0")
	in := newInstallation(t)
	newRelease().serve(t, "desktop-v0.13.0")
	u := in.upgrader("yes\n", true)
	if err := u.Run(context.Background()); err != nil {
		t.Fatalf("%v\n%s", err, u.out)
	}
	journal, _ := readJournal(in.paths)
	if journal.Stage != stageCommitted || journal.Pre == "" || in.file(t, launcherName()) != "new launcher" || in.file(t, "data/state/daedalus.sqlite") != "schema v2\n" {
		t.Fatalf("journal %+v\n%s", journal, u.out)
	}
}

// The new launcher's version does not come up while the launcher that began the upgrade waits for
// it: the finish leaves the rollback to that launcher, which holds the installation lock, and it
// puts both the data and itself back.
func TestAFencedUpgradeThatDoesNotComeUpPutsBothBack(t *testing.T) {
	fencedInstallation(t)
	withVersion(t, "desktop-v0.12.0")
	in := newInstallation(t)
	original := fenceIno(t, in.paths.Data)
	in.stack.fail = true
	newRelease().serve(t, "desktop-v0.13.0")
	u := in.upgrader("yes\n", true)
	if err := u.Run(context.Background()); err == nil {
		t.Fatalf("reported success\n%s", u.out)
	}
	if in.file(t, launcherName()) != "old launcher" || fenceIno(t, in.paths.Data) != original || in.file(t, "data/state/daedalus.sqlite") != "schema v1\n" {
		t.Fatalf("not put back\n%s", u.out)
	}
	if journal, _ := readJournal(in.paths); journal.Stage != stageRolledBack {
		t.Fatalf("journal %+v\n%s", journal, u.out)
	}
	if !strings.Contains(u.out.(interface{ String() string }).String(), "The launcher that began the upgrade puts the data and itself back") {
		t.Fatalf("the finish did not leave the rollback to the first launcher\n%s", u.out)
	}
}

// The launcher that began the upgrade is gone by the time the new version fails: the finish takes
// the free installation lock and rolls back by itself.
func TestAFencedFinishRollsBackItselfWhenTheFirstLauncherIsGone(t *testing.T) {
	fencedInstallation(t)
	withVersion(t, "desktop-v0.12.0")
	in := newInstallation(t)
	original := fenceIno(t, in.paths.Data)
	in.stack.fail = true
	newRelease().serve(t, "desktop-v0.13.0")
	u := in.upgrader("yes\n", true)
	run := u.opts.runNewBinary
	u.opts.runNewBinary = func(ctx context.Context, exe string, args []string) error {
		// The first launcher dies: its installation lock goes with it.
		u.lock.Release()
		return run(ctx, exe, args)
	}
	if err := u.Run(context.Background()); err == nil {
		t.Fatalf("reported success\n%s", u.out)
	}
	if fenceIno(t, in.paths.Data) != original || in.file(t, "data/state/daedalus.sqlite") != "schema v1\n" {
		t.Fatalf("the data is not back\n%s", u.out.(interface{ String() string }).String())
	}
	if strings.Contains(u.out.(interface{ String() string }).String(), "The launcher that began the upgrade puts the data and itself back") {
		t.Fatalf("the finish waited for a launcher that is gone\n%s", u.out)
	}
}

func TestTheProtectionIsChosenByPlatformAndFlag(t *testing.T) {
	fenceRequireExt4(t, os.TempDir())
	p := fixtureData(t)
	for flag, want := range map[string]protection{"": protectFence, "auto": protectFence, "off": protectBackup} {
		t.Setenv("DAEDALUS_DATA_FENCE", flag)
		got, why, err := chooseProtection(p, ModeNative)
		if err != nil || got != want {
			t.Fatalf("%q: %s (%s) %v", flag, got, why, err)
		}
	}
	t.Setenv("DAEDALUS_DATA_FENCE", "auto")
	if got, _, _ := chooseProtection(p, ModeDocker); got != protectBackup {
		t.Fatal("Docker mode was given the fence")
	}
	t.Setenv("DAEDALUS_DATA_FENCE", "yes please")
	if _, _, err := chooseProtection(p, ModeNative); err == nil {
		t.Fatal("an unknown value was accepted")
	}
}

// The kept-copies card, rendered by the page's own script in a headless Chromium, in both
// languages: a kept copy is listed, and a possible loss turns it into the card that needs the
// operator. Only with DAEDALUS_BROWSER_TEST=1; DAEDALUS_KEPT_CARD_SHOT=<prefix> keeps a screenshot per
// language (DAEDALUS_BROWSER_SHOT is the upgrade card's, a whole file name).
func TestTheKeptCopiesCardIsShownInABrowser(t *testing.T) {
	if os.Getenv("DAEDALUS_BROWSER_TEST") != "1" {
		t.Skip("set DAEDALUS_BROWSER_TEST=1 to render the page in a headless Chromium")
	}
	for _, lang := range []string{"en", "ru"} {
		t.Run(lang, func(t *testing.T) {
			data := fenceFixture(t)
			rep := fencedSwitch(fenceOptions{Data: data})
			fenceWant(t, rep, fenceCommitted)
			pre := filepath.Base(fenceRetainedPath(data, rep, "pre"))
			g := fenceGC(t, data, pre, false, &fenceSeams{gcBeforeUnlink: func(r *fenceTree, rel string) {
				if rel == "workspaces/note.txt" {
					fenceOverflowInto(r)
				}
			}})
			if g.Outcome != fenceLostPossible {
				t.Fatalf("setup: %+v", g)
			}
			again := fencedSwitch(fenceOptions{Data: data})
			fenceWant(t, again, fenceCommitted)
			p, _ := NewPaths(data)
			os.WriteFile(p.Env, []byte("X=1\n"), 0o600)
			os.WriteFile(p.Mode, []byte("native\n"), 0o600)
			os.WriteFile(p.Lang, []byte(lang+"\n"), 0o600)
			app := NewApp(p)
			app.SetMode(ModeNative)
			server := NewServer(app, 0)
			if err := server.Start(); err != nil {
				t.Fatal(err)
			}
			defer server.Stop(context.Background())
			shot := ""
			if prefix := os.Getenv("DAEDALUS_KEPT_CARD_SHOT"); prefix != "" {
				shot = prefix + "-" + lang + ".png"
			}
			ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
			defer cancel()
			out, err := exec.CommandContext(ctx, "python3", "-c", keptCopiesScript, server.URL()+"status", shot).CombinedOutput()
			if err != nil {
				t.Fatalf("%v\n%s", err, out)
			}
			got := string(out)
			t.Logf("%s", got)
			want := map[string][]string{
				"en": {"Something here needs you", "A write may have been lost", "Kept: ", "daedalus-desktop update status"},
				"ru": {"Здесь нужно ваше внимание", "запись могла потеряться", "Сохранено: ", "daedalus-desktop update status"},
			}[lang]
			if !strings.Contains(got, "visible=True") {
				t.Fatalf("the card is not visible:\n%s", got)
			}
			for _, line := range want {
				if !strings.Contains(got, line) {
					t.Fatalf("%q is not on the card:\n%s", line, got)
				}
			}
		})
	}
}

const keptCopiesScript = `
import sys
from playwright.sync_api import sync_playwright
url, shot = sys.argv[1], sys.argv[2]
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1000, "height": 1300})
    page.goto(url)
    page.wait_for_selector("#switches:not([hidden])", timeout=15000)
    page.wait_for_function("getComputedStyle(document.getElementById('switches')).opacity === '1'", timeout=15000)
    card = page.locator("#switches")
    print("visible=%s" % card.is_visible())
    print("trouble=%s" % ("decide" in (card.get_attribute("class") or "")))
    print(card.inner_text())
    if shot:
        page.screenshot(path=shot, full_page=True)
    browser.close()
`
