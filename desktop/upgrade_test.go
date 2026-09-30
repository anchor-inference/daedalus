package main

import (
	"archive/tar"
	"archive/zip"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

// These tests run a whole upgrade against a fixture: an installation folder in a temporary
// directory, a release server on this machine, and a stack that does to the data what a migration
// would. The new launcher is simulated by running --finish in-process under the new version.

type archiveEntry struct {
	name, body, link string
	dir              bool
}

func makeArchive(t *testing.T, name string, entries []archiveEntry) []byte {
	t.Helper()
	var buf bytes.Buffer
	if strings.HasSuffix(name, ".zip") {
		writer := zip.NewWriter(&buf)
		for _, e := range entries {
			header := &zip.FileHeader{Name: e.name, Method: zip.Deflate}
			switch {
			case e.link != "":
				header.SetMode(os.ModeSymlink | 0o777)
			case e.dir:
				header.Name += "/"
				header.SetMode(os.ModeDir | 0o755)
			default:
				header.SetMode(0o755)
			}
			w, err := writer.CreateHeader(header)
			if err != nil {
				t.Fatal(err)
			}
			if e.link != "" {
				w.Write([]byte(e.link))
			} else {
				w.Write([]byte(e.body))
			}
		}
		writer.Close()
		return buf.Bytes()
	}
	zipped := gzip.NewWriter(&buf)
	writer := tar.NewWriter(zipped)
	for _, e := range entries {
		header := &tar.Header{Name: e.name, Mode: 0o755, Typeflag: tar.TypeReg, Size: int64(len(e.body))}
		if e.link != "" {
			header.Typeflag, header.Linkname, header.Size = tar.TypeSymlink, e.link, 0
		} else if e.dir {
			header.Typeflag, header.Name, header.Size = tar.TypeDir, e.name+"/", 0
		}
		writer.WriteHeader(header)
		if header.Typeflag == tar.TypeReg {
			writer.Write([]byte(e.body))
		}
	}
	writer.Close()
	zipped.Close()
	return buf.Bytes()
}

// launcherName is the executable's place inside an installation on this platform.
func launcherName() string {
	switch runtime.GOOS {
	case "darwin":
		return "Daedalus.app/Contents/MacOS/daedalus-desktop"
	case "windows":
		return "daedalus-desktop.exe"
	}
	return "daedalus-desktop"
}

type fixtureRelease struct {
	entries  []archiveEntry
	badSum   bool
	requests int
}

func (f *fixtureRelease) serve(t *testing.T, tag string) {
	t.Helper()
	asset, err := platformAsset(runtime.GOOS, runtime.GOARCH)
	if err != nil {
		t.Skip(err)
	}
	archive := makeArchive(t, asset, f.entries)
	sum := sha256.Sum256(archive)
	digest := hex.EncodeToString(sum[:])
	if f.badSum {
		digest = strings.Repeat("0", 64)
	}
	var server *httptest.Server
	server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		f.requests++
		switch r.URL.Path {
		case "/releases":
			json.NewEncoder(w).Encode([]map[string]any{{
				"tag_name": tag, "html_url": server.URL + "/notes",
				"assets": []map[string]any{
					{"name": asset, "browser_download_url": server.URL + "/download/" + asset},
					{"name": "SHA256SUMS", "browser_download_url": server.URL + "/download/SHA256SUMS"},
				},
			}})
		case "/download/" + asset:
			w.Write(archive)
		case "/download/SHA256SUMS":
			fmt.Fprintf(w, "%s  %s\n", digest, asset)
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(server.Close)
	t.Setenv("DAEDALUS_RELEASES_API", server.URL)
}

func newRelease() *fixtureRelease {
	return &fixtureRelease{entries: []archiveEntry{
		{name: launcherName(), body: "new launcher"},
		{name: "ptyd", body: "new ptyd"},
		{name: "miniapp-dist", dir: true},
		{name: "miniapp-dist/index.html", body: "new app"},
	}}
}

// fakeStack records what the upgrade asked of it and migrates the data on UpdateAndCheck.
type fakeStack struct {
	onUpdate  func()
	p         Paths
	fail      bool
	stops     int
	updates   int
	restores  int
	snapshots int
	leaves    int
}

func (s *fakeStack) Configured() bool           { return true }
func (s *fakeStack) Stop(context.Context) error { s.stops++; return nil }
func (s *fakeStack) Leave(context.Context)      { s.leaves++ }
func (s *fakeStack) Snapshot(context.Context, string, *Manifest) error {
	s.snapshots++
	return nil
}
func (s *fakeStack) Restore(context.Context, string, *Manifest) error { s.restores++; return nil }
func (s *fakeStack) Prepare(context.Context) error                    { return nil }

func (s *fakeStack) UpdateAndCheck(context.Context) error {
	s.updates++
	if s.onUpdate != nil {
		s.onUpdate()
	}
	os.WriteFile(filepath.Join(s.p.State, "daedalus.sqlite"), []byte("schema v2\n"), 0o640)
	os.WriteFile(filepath.Join(s.p.State, "added-by-migration"), []byte("x"), 0o640)
	if s.fail {
		return errors.New("the app did not answer")
	}
	return nil
}

type installation struct {
	root  string
	exe   string
	paths Paths
	stack *fakeStack
}

func newInstallation(t *testing.T) *installation {
	t.Helper()
	root := filepath.Join(t.TempDir(), "Daedalus")
	exe := filepath.Join(root, filepath.FromSlash(launcherName()))
	for name, body := range map[string]string{
		exe:                         "old launcher",
		filepath.Join(root, "ptyd"): "old ptyd",
		filepath.Join(root, "miniapp-dist", "index.html"): "old app",
	} {
		os.MkdirAll(filepath.Dir(name), 0o755)
		if err := os.WriteFile(name, []byte(body), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	data := fixtureData(t)
	// fixtureData made it elsewhere; the default layout has it beside the launcher.
	if err := os.Rename(data.Data, filepath.Join(root, "data")); err != nil {
		t.Fatal(err)
	}
	paths, _ := NewPaths(filepath.Join(root, "data"))
	return &installation{root: root, exe: exe, paths: paths, stack: &fakeStack{p: paths}}
}

func (in *installation) upgrader(answer string, tty bool) *Upgrader {
	u := &Upgrader{paths: in.paths, stack: in.stack, mode: ModeNative, out: &bytes.Buffer{}, opts: upgradeOptions{
		executable: in.exe, stdin: strings.NewReader(answer), stdinIsTTY: tty,
		now: func() time.Time { return time.Unix(1_800_000_000, 0) },
	}}
	// The new launcher, simulated: it is the file now in place, and it runs --finish as its version.
	u.opts.runNewBinary = func(ctx context.Context, exe string, args []string) error {
		if exe != in.exe {
			return fmt.Errorf("ran %s, not %s", exe, in.exe)
		}
		if body, _ := os.ReadFile(exe); string(body) != "new launcher" {
			return fmt.Errorf("%s is %q, not the new launcher", exe, body)
		}
		old := version
		version = "desktop-v0.13.0"
		defer func() { version = old }()
		next := &Upgrader{paths: in.paths, stack: in.stack, mode: ModeNative, out: u.out, opts: upgradeOptions{finish: true, now: u.opts.now, finishLock: u.finishLock}}
		return next.Run(ctx)
	}
	return u
}

func (in *installation) file(t *testing.T, name string) string {
	t.Helper()
	body, err := os.ReadFile(filepath.Join(in.root, filepath.FromSlash(name)))
	if err != nil {
		return "<missing>"
	}
	return string(body)
}

func TestAnUpgradeKeepsTheMigratedDataWhenItWorks(t *testing.T) {
	withVersion(t, "desktop-v0.12.0")
	in := newInstallation(t)
	newRelease().serve(t, "desktop-v0.13.0")
	u := in.upgrader("yes\n", true)
	if err := u.Run(context.Background()); err != nil {
		t.Fatalf("%v\n%s", err, u.out)
	}
	if got := in.file(t, launcherName()); got != "new launcher" {
		t.Fatalf("the launcher is %q", got)
	}
	if in.file(t, "ptyd") != "new ptyd" || in.file(t, "miniapp-dist/index.html") != "new app" {
		t.Fatal("the release's other files are not in place")
	}
	if got := in.file(t, "data/state/daedalus.sqlite"); got != "schema v2\n" {
		t.Fatalf("the migrated database is %q; a successful upgrade keeps it", got)
	}
	journal, _ := readJournal(in.paths)
	if journal.Stage != stageCommitted || in.stack.stops == 0 || in.stack.updates != 1 || in.stack.snapshots != 1 {
		t.Fatalf("journal %+v, stack %+v", journal, in.stack)
	}
	if _, err := VerifyBackup(journal.Backup); err != nil {
		t.Fatalf("the backup kept after the upgrade does not verify: %v", err)
	}
	if body, _ := os.ReadFile(filepath.Join(journal.Work, "old", filepath.FromSlash(strings.Split(launcherName(), "/")[0]))); runtime.GOOS != "darwin" && string(body) != "old launcher" {
		t.Fatalf("the previous launcher was not kept: %q", body)
	}
	if err := InterruptedUpgrade(in.paths); err != nil {
		t.Fatalf("a committed upgrade blocks the launcher: %v", err)
	}
}

func TestAFailedHealthCheckPutsBothTheDataAndTheLauncherBack(t *testing.T) {
	withVersion(t, "desktop-v0.12.0")
	in := newInstallation(t)
	in.stack.fail = true
	newRelease().serve(t, "desktop-v0.13.0")
	u := in.upgrader("yes\n", true)
	err := u.Run(context.Background())
	if err == nil {
		t.Fatal("a failed upgrade reported success")
	}
	if got := in.file(t, launcherName()); got != "old launcher" {
		t.Fatalf("the launcher is %q after the rollback", got)
	}
	if in.file(t, "ptyd") != "old ptyd" || in.file(t, "miniapp-dist/index.html") != "old app" {
		t.Fatal("the release's other files were not put back")
	}
	if got := in.file(t, "data/state/daedalus.sqlite"); got != "schema v1\n" {
		t.Fatalf("the database is %q after the rollback", got)
	}
	if exists(filepath.Join(in.paths.State, "added-by-migration")) {
		t.Fatal("what the migration added survived the rollback")
	}
	if in.file(t, "data/daedalus-secrets/keyproxy.env") == "<missing>" {
		t.Fatal("the keys were lost")
	}
	journal, _ := readJournal(in.paths)
	if journal.Stage != stageRolledBack || in.stack.restores != 1 {
		t.Fatalf("journal %+v, stack %+v", journal, in.stack)
	}
	// The migrated data is not deleted, only moved aside.
	aside, _ := filepath.Glob(filepath.Join(upgradeDir(in.paths), "replaced-*", "state", "daedalus.sqlite"))
	if len(aside) != 1 {
		t.Fatalf("the replaced data was not kept aside: %v", aside)
	}
	if err := InterruptedUpgrade(in.paths); err != nil {
		t.Fatalf("a rolled-back upgrade blocks the launcher: %v", err)
	}
}

func TestANewLauncherThatDoesNotRunIsTakenBackOut(t *testing.T) {
	withVersion(t, "desktop-v0.12.0")
	in := newInstallation(t)
	newRelease().serve(t, "desktop-v0.13.0")
	u := in.upgrader("yes\n", true)
	u.opts.runNewBinary = func(context.Context, string, []string) error { return errors.New("exec format error") }
	if err := u.Run(context.Background()); err == nil {
		t.Fatal("reported success")
	}
	if in.file(t, launcherName()) != "old launcher" || in.file(t, "data/state/daedalus.sqlite") != "schema v1\n" {
		t.Fatal("not put back")
	}
	if journal, _ := readJournal(in.paths); journal.Stage != stageRolledBack {
		t.Fatalf("stage %s", journal.Stage)
	}
}

func TestNothingChangesWithoutAYes(t *testing.T) {
	withVersion(t, "desktop-v0.12.0")
	for _, c := range []struct {
		answer string
		tty    bool
	}{{"no\n", true}, {"\n", true}, {"yes\n", false}} {
		in := newInstallation(t)
		release := newRelease()
		release.serve(t, "desktop-v0.13.0")
		if err := in.upgrader(c.answer, c.tty).Run(context.Background()); err == nil {
			t.Fatalf("%+v: upgraded", c)
		}
		if in.file(t, launcherName()) != "old launcher" || exists(backupsDir(in.paths)) || in.stack.stops != 0 {
			t.Fatalf("%+v: something changed", c)
		}
	}
}

func TestABrokenDownloadChangesNothing(t *testing.T) {
	withVersion(t, "desktop-v0.12.0")
	in := newInstallation(t)
	release := newRelease()
	release.badSum = true
	release.serve(t, "desktop-v0.13.0")
	if err := in.upgrader("yes\n", true).Run(context.Background()); err == nil || !strings.Contains(err.Error(), "checksum") {
		t.Fatalf("err = %v", err)
	}
	if in.file(t, launcherName()) != "old launcher" || exists(backupsDir(in.paths)) || in.stack.stops != 0 {
		t.Fatal("something changed")
	}
}

func TestAReleaseArchiveCannotReachOutside(t *testing.T) {
	withVersion(t, "desktop-v0.12.0")
	for _, bad := range [][]archiveEntry{
		{{name: launcherName(), body: "new launcher"}, {name: "../escaped", body: "x"}},
		{{name: launcherName(), body: "new launcher"}, {name: "evil", link: "/etc/passwd"}},
		{{name: launcherName(), body: "new launcher"}, {name: "data/state/daedalus.sqlite", body: "overwritten"}},
		{{name: "ptyd", body: "no launcher in here"}},
	} {
		in := newInstallation(t)
		release := &fixtureRelease{entries: bad}
		release.serve(t, "desktop-v0.13.0")
		if err := in.upgrader("yes\n", true).Run(context.Background()); err == nil {
			t.Fatalf("%v: accepted", bad)
		}
		if in.file(t, launcherName()) != "old launcher" || in.file(t, "data/state/daedalus.sqlite") != "schema v1\n" || exists(filepath.Join(in.root, "..", "escaped")) {
			t.Fatalf("%v: something changed", bad)
		}
	}
}

func TestAFailedRollbackStopsAndBlocksTheLauncher(t *testing.T) {
	withVersion(t, "desktop-v0.12.0")
	in := newInstallation(t)
	in.stack.fail = true
	newRelease().serve(t, "desktop-v0.13.0")
	u := in.upgrader("yes\n", true)
	finish := u.opts.runNewBinary
	u.opts.runNewBinary = func(ctx context.Context, exe string, args []string) error {
		// The backup is damaged after it was verified: the rollback must refuse it, not restore it.
		journal, _ := readJournal(in.paths)
		os.WriteFile(filepath.Join(journal.Backup, backupArchive), []byte("damaged"), 0o600)
		return finish(ctx, exe, args)
	}
	err := u.Run(context.Background())
	if err == nil || !strings.Contains(err.Error(), "Do not start the launcher") {
		t.Fatalf("err = %v", err)
	}
	journal, _ := readJournal(in.paths)
	if journal.Stage != stageRollbackFailed {
		t.Fatalf("stage %s", journal.Stage)
	}
	if err := InterruptedUpgrade(in.paths); err == nil || !strings.Contains(err.Error(), "--rollback") {
		t.Fatalf("the launcher is not blocked: %v", err)
	}
	// The data the failed upgrade left is not deleted by a rollback that could not restore.
	if in.file(t, "data/state/daedalus.sqlite") != "schema v2\n" {
		t.Fatal("the data was touched by a rollback that refused its backup")
	}
}

func TestAnInterruptedUpgradeIsRolledBackOnRequest(t *testing.T) {
	withVersion(t, "desktop-v0.12.0")
	in := newInstallation(t)
	newRelease().serve(t, "desktop-v0.13.0")
	u := in.upgrader("yes\n", true)
	// The new launcher gets as far as the migration and the machine loses power.
	u.opts.runNewBinary = func(ctx context.Context, exe string, args []string) error {
		journal, _ := readJournal(in.paths)
		journal.Stage = stageFinishing
		writeJournal(in.paths, journal)
		in.stack.UpdateAndCheck(ctx)
		panic("power cut")
	}
	func() {
		defer func() { recover() }()
		u.Run(context.Background())
	}()
	if err := InterruptedUpgrade(in.paths); err == nil {
		t.Fatal("an interrupted upgrade does not block the launcher")
	}
	// The next command is the new launcher's (it is the file in place), run by the operator.
	rollback := &Upgrader{paths: in.paths, stack: in.stack, mode: ModeNative, out: &bytes.Buffer{}, opts: upgradeOptions{rollback: true}}
	if err := rollback.Run(context.Background()); err == nil || !strings.Contains(err.Error(), "rolled back") {
		t.Fatalf("err = %v", err)
	}
	if in.file(t, launcherName()) != "old launcher" || in.file(t, "data/state/daedalus.sqlite") != "schema v1\n" {
		t.Fatal("not put back")
	}
	if err := InterruptedUpgrade(in.paths); err != nil {
		t.Fatal(err)
	}
}

func TestARunningLauncherIsNotUpgradedUnderIt(t *testing.T) {
	withVersion(t, "desktop-v0.12.0")
	in := newInstallation(t)
	newRelease().serve(t, "desktop-v0.13.0")
	os.WriteFile(instanceFile(in.paths), []byte(fmt.Sprintf(`{"port":1,"token":"t","pid":%d}`, os.Getpid())), 0o600)
	if err := in.upgrader("yes\n", true).Run(context.Background()); err == nil || !strings.Contains(err.Error(), "close it first") {
		t.Fatalf("err = %v", err)
	}
}

func TestDockerModeIsRefused(t *testing.T) {
	withVersion(t, "desktop-v0.12.0")
	in := newInstallation(t)
	release := newRelease()
	release.serve(t, "desktop-v0.13.0")
	u := in.upgrader("yes\n", true)
	u.mode = ModeDocker
	if err := u.Run(context.Background()); err == nil || !strings.Contains(err.Error(), "Docker installation from the launcher is not available yet") {
		t.Fatalf("err = %v", err)
	}
	if release.requests != 0 {
		t.Fatal("the refusal came after the network")
	}
}

func TestDockerVolumesAreArchivedAndRestoredByTheRecordedImage(t *testing.T) {
	backup := t.TempDir()
	var calls [][]string
	docker := func(ctx context.Context, args ...string) (string, error) {
		calls = append(calls, args)
		switch {
		case args[0] == "image" && args[1] == "inspect":
			return "sha256:" + args[len(args)-1][len(args[len(args)-1])-6:] + "\n", nil
		case args[0] == "volume":
			return "daedalus_daedalus-state\n", nil
		case args[0] == "run":
			// What the container would have written.
			name := filepath.Join(backup, backupVolumes, "daedalus_daedalus-state.tar.gz")
			os.WriteFile(name, makeArchive(t, "v.tar.gz", []archiveEntry{{name: "daedalus.sqlite", body: "v1"}}), 0o600)
		}
		return "", nil
	}
	manifest := &Manifest{}
	if err := snapshotDocker(context.Background(), backup, manifest, docker); err != nil {
		t.Fatal(err)
	}
	if len(manifest.Volumes) != 1 || manifest.Volumes[0].Entries != 1 || manifest.Images[agentImage()] == "" {
		t.Fatalf("manifest %+v", manifest)
	}
	run := calls[len(calls)-1]
	joined := strings.Join(run, " ")
	if !strings.Contains(joined, "daedalus_daedalus-state:/from:ro") || !strings.Contains(joined, "--network none") {
		t.Fatalf("archived with %v", run)
	}
	calls = nil
	if err := restoreDocker(context.Background(), backup, manifest, docker); err != nil {
		t.Fatal(err)
	}
	joined = fmt.Sprint(calls)
	if !strings.Contains(joined, manifest.Images[agentImage()]) || !strings.Contains(joined, "image tag") {
		t.Fatalf("restored with %v", calls)
	}
}

func TestASwapCutOffHalfwayIsPutBack(t *testing.T) {
	withVersion(t, "desktop-v0.12.0")
	in := newInstallation(t)
	newRelease().serve(t, "desktop-v0.13.0")
	u := in.upgrader("yes\n", true)
	// The machine stops after the first old item moved aside and before the new one moved in: the
	// installation has no launcher at all, and the journal is all there is to go by.
	u.opts.runNewBinary = func(context.Context, string, []string) error { panic("unreachable") }
	journal := func() *Journal { j, _ := readJournal(in.paths); return j }
	func() {
		defer func() { recover() }()
		hook := writeJournalHook
		writeJournalHook = func(j *Journal) {
			if j.Stage == stageBackedUp && len(j.Swapped) == 1 {
				panic("power cut")
			}
		}
		defer func() { writeJournalHook = hook }()
		u.Run(context.Background())
	}()
	if j := journal(); j.Stage != stageBackedUp || len(j.Swapped) != 1 {
		t.Fatalf("journal %+v", j)
	}
	if err := InterruptedUpgrade(in.paths); err == nil {
		t.Fatal("a half-done swap does not block the launcher")
	}
	rollback := &Upgrader{paths: in.paths, stack: in.stack, mode: ModeNative, out: &bytes.Buffer{}, opts: upgradeOptions{rollback: true}}
	if err := rollback.Run(context.Background()); err == nil || !strings.Contains(err.Error(), "rolled back") {
		t.Fatalf("err = %v", err)
	}
	if in.file(t, launcherName()) != "old launcher" || in.file(t, "ptyd") != "old ptyd" || in.file(t, "data/state/daedalus.sqlite") != "schema v1\n" {
		t.Fatal("not put back")
	}
	if err := InterruptedUpgrade(in.paths); err != nil {
		t.Fatal(err)
	}
}

func TestAJournalThatIsNotAnUpgradeDoesNotBlock(t *testing.T) {
	p, _ := NewPaths(t.TempDir())
	if err := InterruptedUpgrade(p); err != nil {
		t.Fatal(err)
	}
	for _, stage := range []string{stagePrepared, stageCommitted, stageRolledBack} {
		writeJournal(p, &Journal{Stage: stage})
		if err := InterruptedUpgrade(p); err != nil {
			t.Fatalf("%s blocks: %v", stage, err)
		}
	}
	for _, stage := range []string{stageBackedUp, stageSwapped, stageFinishing, stageRollbackFailed} {
		writeJournal(p, &Journal{Stage: stage})
		if err := InterruptedUpgrade(p); err == nil {
			t.Fatalf("%s does not block", stage)
		}
	}
}
