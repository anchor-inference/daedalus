//go:build !windows

package main

// The Go smoke: the whole mechanism on real processes, end to end, on this machine only. It builds
// the v0.12.0 launcher from its own tag's source and two new launchers from this tree (tagged
// upgradefixture, so the stack an upgrade starts is fixture_stack.go), serves them as releases from
// an httptest server on loopback, installs with install.sh, and runs the launchers as separate
// processes — in parallel, killed with SIGKILL, interrupted with SIGINT to their process group as a
// terminal's Ctrl+C is. Everything is in temporary folders; no container, no real data, no port
// but the loopback ones it picks.
//
//   DAEDALUS_SMOKE=1 go test -tags nowebview -run TestSmoke -v .      (Linux; needs git, curl, sh)

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io/fs"
	"math/rand"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"
)

type smoke struct {
	t      *testing.T
	work   string
	bins   map[string]string
	server *httptest.Server
	mu     sync.Mutex
	tags   []string // newest first, as GitHub lists them
	files  map[string][]byte
	// installer is install.sh carrying the test's release key where the project's is: every release
	// here is signed with that key, and the new launchers are built to trust it and nothing else.
	installer string
}

func TestSmoke(t *testing.T) {
	if os.Getenv("DAEDALUS_SMOKE") != "1" {
		t.Skip("set DAEDALUS_SMOKE=1: builds three launchers and runs them as processes")
	}
	if runtime.GOOS != "linux" {
		t.Skip("the Linux release shape")
	}
	s := newSmoke(t)
	t.Run("bridge v0.12.0 → v0.13.0 works", s.bridgeWorks)
	t.Run("bridge whose health check fails puts everything back", s.bridgeFails)
	t.Run("two upgrades at once", s.twoUpgradesAtOnce)
	t.Run("kill -9 while migrating, then --rollback", s.killedWhileMigrating)
	t.Run("Ctrl+C while migrating", s.interruptedWhileMigrating)
	t.Run("Ctrl+C while rolling back", s.interruptedWhileRollingBack)
	t.Run("kill -9 of the old launcher while migrating", s.parentKilledWhileMigrating)
	t.Run("SIGHUP to both launchers while migrating", s.hangupWhileMigrating)
	t.Run("kill -9 of the old launcher just before the hand-over", s.parentKilledBeforeHandover)
	t.Run("kill -9 of the old launcher just after the hand-over", s.parentKilledAfterHandover)
	t.Run("kill -9 of the old launcher at random moments, with a rollback racing it", s.randomParentKills)
	t.Run("--finish killed while its stack runs, old launcher rolls back", s.finishKilledStackRunning)
	t.Run("both launchers killed while the stack runs, then --rollback", s.bothKilledStackRunning)
	t.Run("a process nobody can confirm on the app's port: rollback refuses to restore", s.unconfirmedStackRefusesRestore)
	t.Run("update: backup first, rollback on failure", s.update)
	t.Run("install.sh over a launcher that has upgrade", s.installerHandsOver)
}

func newSmoke(t *testing.T) *smoke {
	work := os.Getenv("SMOKE_DIR")
	if work == "" {
		work = t.TempDir()
	}
	s := &smoke{t: t, work: work, bins: map[string]string{}, files: map[string][]byte{}}
	bin := filepath.Join(work, "bin")
	os.MkdirAll(bin, 0o755)
	env := append(os.Environ(), "CGO_ENABLED=0", "GOTOOLCHAIN=local", "GOPROXY=off")
	// v0.12.0 from its tag, exactly as released.
	src := filepath.Join(work, "src-v0.12.0")
	os.MkdirAll(src, 0o755)
	archive := exec.Command("sh", "-c", "git -C .. archive desktop-v0.12.0 desktop | tar -x -C "+src)
	if out, err := archive.CombinedOutput(); err != nil {
		t.Fatalf("git archive: %v\n%s", err, out)
	}
	build := func(dir, tags, tag string) {
		out := filepath.Join(bin, tag)
		cmd := exec.Command("go", "build", "-tags", tags, "-trimpath", "-ldflags", "-X main.version="+tag+" -X main.linkedFixtureReleaseKey="+keyLine(testReleaseKey), "-o", out, ".")
		cmd.Dir, cmd.Env = dir, env
		if b, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("build %s: %v\n%s", tag, err, b)
		}
		s.bins[tag] = out
	}
	build(filepath.Join(src, "desktop"), "nowebview", "desktop-v0.12.0")
	build(".", "nowebview,upgradefixture", "desktop-v0.13.0")
	build(".", "nowebview,upgradefixture", "desktop-v0.14.0")
	if help, _ := exec.Command(s.bins["desktop-v0.12.0"], "--help").Output(); strings.Contains(string(help), "\n  upgrade ") {
		t.Fatal("the v0.12.0 build knows upgrade")
	}
	s.installer = installerWithKey(t, keyLine(testReleaseKey))
	s.server = httptest.NewServer(http.HandlerFunc(s.serve))
	t.Cleanup(s.server.Close)
	return s
}

func (s *smoke) serve(w http.ResponseWriter, r *http.Request) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if r.URL.Path == "/releases" {
		var list []map[string]any
		for _, tag := range s.tags {
			list = append(list, map[string]any{"tag_name": tag, "draft": false, "prerelease": false, "html_url": s.server.URL + "/notes/" + tag,
				"assets": []map[string]any{
					{"name": smokeAsset(), "browser_download_url": s.server.URL + "/download/" + tag + "/" + smokeAsset()},
					{"name": "SHA256SUMS", "browser_download_url": s.server.URL + "/download/" + tag + "/SHA256SUMS"},
					{"name": signatureAsset, "browser_download_url": s.server.URL + "/download/" + tag + "/" + signatureAsset},
				}})
		}
		json.NewEncoder(w).Encode(list)
		return
	}
	if body, ok := s.files[r.URL.Path]; ok {
		w.Write(body)
		return
	}
	http.NotFound(w, r)
}

func smokeAsset() string { return "daedalus-desktop-linux-" + runtime.GOARCH + ".tar.gz" }

// publish makes tag the newest release: the launcher and three companion files, as the workflow packs them.
func (s *smoke) publish(tag string) {
	exe, err := os.ReadFile(s.bins[tag])
	if err != nil {
		s.t.Fatal(err)
	}
	var buf bytes.Buffer
	zw := gzip.NewWriter(&buf)
	tw := tar.NewWriter(zw)
	add := func(name string, body []byte, mode int64) {
		tw.WriteHeader(&tar.Header{Name: name, Mode: mode, Size: int64(len(body)), Typeflag: tar.TypeReg})
		tw.Write(body)
	}
	add("daedalus-desktop", exe, 0o755)
	add("ptyd", []byte("fixture ptyd "+tag+"\n"), 0o755)
	add("browserd", []byte("fixture browserd "+tag+"\n"), 0o755)
	tw.WriteHeader(&tar.Header{Name: "miniapp-dist/", Mode: 0o755, Typeflag: tar.TypeDir})
	add("miniapp-dist/index.html", []byte("<p>"+tag+"</p>\n"), 0o644)
	tw.Close()
	zw.Close()
	sum := sha256.Sum256(buf.Bytes())
	s.mu.Lock()
	s.files["/download/"+tag+"/"+smokeAsset()] = buf.Bytes()
	sums := []byte(hex.EncodeToString(sum[:]) + "  " + smokeAsset() + "\n")
	s.files["/download/"+tag+"/SHA256SUMS"] = sums
	s.files["/download/"+tag+"/"+signatureAsset] = testReleaseSign(releaseMessage(tag, sums))
	s.tags = append([]string{tag}, s.tags...)
	s.mu.Unlock()
}

func (s *smoke) unpublish(tag string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for i, t := range s.tags {
		if t == tag {
			s.tags = append(s.tags[:i], s.tags[i+1:]...)
			return
		}
	}
}

func (s *smoke) env(extra ...string) []string {
	env := []string{}
	for _, kv := range os.Environ() {
		// The test's own local root is kept, so that no launcher of the smoke writes its runtime
		// or its state into this user's real cache and state folders.
		if strings.HasPrefix(kv, "DAEDALUS_") && !strings.HasPrefix(kv, "DAEDALUS_LOCAL_ROOT=") {
			continue
		}
		env = append(env, kv)
	}
	// The smoke checks the verified backup byte for byte, which is what Windows and macOS use; the
	// fenced switch is exercised end to end by protect_linux_test.go and on a real installation.
	env = append(env, "DAEDALUS_DATA_FENCE=off")
	tmp := filepath.Join(s.work, "tmp")
	os.MkdirAll(tmp, 0o700)
	env = append(env, "TMPDIR="+tmp)
	return append(append(env, "DAEDALUS_UPDATE_CHECK=off", "DAEDALUS_RELEASES_API="+s.server.URL, "DAEDALUS_DOWNLOAD_BASE="+s.server.URL+"/download"), extra...)
}

// install runs install.sh for a named installation with no terminal.
func (s *smoke) install(name string, extra ...string) (string, error) {
	cmd := exec.Command("setsid", "sh", s.installer)
	cmd.Env = s.env(append([]string{"DAEDALUS_DIR=" + filepath.Join(s.work, name, "Daedalus")}, extra...)...)
	cmd.Stdin = nil
	out, err := cmd.CombinedOutput()
	return string(out), err
}

// fresh installs the newest published release into a new installation and gives it data.
func (s *smoke) fresh(name string, bigMB int) (root, data string) {
	if out, err := s.install(name); err != nil {
		s.t.Fatalf("install %s: %v\n%s", name, err, out)
	}
	root = filepath.Join(s.work, name, "Daedalus")
	data = filepath.Join(root, "data")
	for rel, body := range map[string]string{
		".upgrade-fixture": "", ".env": "DAEDALUS_PORT=18000\n", "mode": "native\n", "state/daedalus.sqlite": "schema v1\n",
		"daedalus-secrets/keyproxy.env": "FIXTURE_KEY=not-a-real-key\n", "workspaces/notes/today.md": "a note\n", "daedalus/.git/HEAD": "ref: refs/heads/main\n",
	} {
		os.MkdirAll(filepath.Dir(filepath.Join(data, rel)), 0o755)
		os.WriteFile(filepath.Join(data, rel), []byte(body), 0o640)
	}
	os.Symlink("today.md", filepath.Join(data, "workspaces", "notes", "latest.md"))
	if bigMB > 0 {
		chunk := bytes.Repeat([]byte{0}, 1<<20)
		os.MkdirAll(filepath.Join(data, "workspaces", "big"), 0o755)
		for i := 0; i < bigMB; i++ {
			copy(chunk, fmt.Sprintf("file %d", i))
			os.WriteFile(filepath.Join(data, "workspaces", "big", fmt.Sprintf("f%03d.bin", i)), chunk, 0o644)
		}
	}
	return root, data
}

// launcher runs the installation's own launcher and returns its output.
func (s *smoke) launcher(root string, extra []string, args ...string) (string, error) {
	cmd := exec.Command(filepath.Join(root, "daedalus-desktop"), args...)
	cmd.Env = s.env(extra...)
	out, err := cmd.CombinedOutput()
	return string(out), err
}

// start runs the launcher in a process group of its own, as a terminal runs a foreground job.
func (s *smoke) start(root string, extra []string, args ...string) (*exec.Cmd, *bytes.Buffer) {
	cmd := exec.Command(filepath.Join(root, "daedalus-desktop"), args...)
	cmd.Env = s.env(extra...)
	var out bytes.Buffer
	cmd.Stdout, cmd.Stderr = &out, &out
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if err := cmd.Start(); err != nil {
		s.t.Fatal(err)
	}
	s.t.Cleanup(func() { _ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL) })
	return cmd, &out
}

func (s *smoke) version(root string) string {
	out, _ := exec.Command(filepath.Join(root, "daedalus-desktop"), "--version").Output()
	return strings.TrimSpace(string(out))
}

func journalOf(data string) Journal {
	var j Journal
	body, _ := os.ReadFile(filepath.Join(fenceControlPath(data), "upgrade.json"))
	json.Unmarshal(body, &j)
	return j
}

func smokeWaitFor(t *testing.T, what string, timeout time.Duration, ok func() bool) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for !ok() {
		if time.Now().After(deadline) {
			t.Fatalf("timed out waiting for %s", what)
		}
		time.Sleep(20 * time.Millisecond)
	}
}

// treeOf is every file, link and folder under dir with its content hash, minus what is skipped.
func treeOf(t *testing.T, dir string, skip ...string) string {
	t.Helper()
	var lines []string
	filepath.WalkDir(dir, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		rel, _ := filepath.Rel(dir, path)
		for _, s := range skip {
			if rel == s || strings.HasPrefix(rel, s+string(os.PathSeparator)) {
				if d.IsDir() {
					return filepath.SkipDir
				}
				return nil
			}
		}
		info, _ := os.Lstat(path)
		switch {
		case info.Mode()&os.ModeSymlink != 0:
			link, _ := os.Readlink(path)
			lines = append(lines, rel+" -> "+link)
		case info.IsDir():
			lines = append(lines, rel+"/")
		default:
			sum, _, _ := sha256File(path)
			lines = append(lines, fmt.Sprintf("%s %s %o", rel, sum, info.Mode().Perm()))
		}
		return nil
	})
	sort.Strings(lines)
	return strings.Join(lines, "\n")
}

var dataSkip = []string{"backups", "upgrade", "runtime"}
var rootSkip = []string{".daedalus-upgrade", ".daedalus-update", "data/backups", "data/upgrade", "data/runtime"}

func (s *smoke) bridgeWorks(t *testing.T) {
	s.publish("desktop-v0.12.0")
	root, data := s.fresh("bridge-ok", 0)
	if v := s.version(root); v != "desktop-v0.12.0" {
		t.Fatalf("installed %s", v)
	}
	s.publish("desktop-v0.13.0")
	out, err := s.install("bridge-ok", "DAEDALUS_UPGRADE_YES=1", "DAEDALUS_UPGRADE_FIXTURE=ok")
	if err != nil || !strings.Contains(out, "predates upgrade") {
		t.Fatalf("%v\n%s", err, out)
	}
	if v := s.version(root); v != "desktop-v0.13.0" {
		t.Fatalf("launcher %s", v)
	}
	j := journalOf(data)
	if j.Stage != stageCommitted || j.From != "desktop-v0.12.0" {
		t.Fatalf("journal %+v", j)
	}
	manifest, err := VerifyBackup(j.Backup)
	if err != nil {
		t.Fatal(err)
	}
	old, _, _ := sha256File(s.bins["desktop-v0.12.0"])
	found := false
	for _, e := range manifest.LauncherEntries {
		found = found || (e.Path == "daedalus-desktop" && e.SHA256 == old)
	}
	if !found {
		t.Fatal("the v0.12.0 launcher is not in the backup byte for byte")
	}
	body, _ := os.ReadFile(filepath.Join(data, "state", "daedalus.sqlite"))
	if string(body) != "schema migrated by desktop-v0.13.0\n" {
		t.Fatalf("data %q", body)
	}
	s.unpublish("desktop-v0.13.0")
}

func (s *smoke) bridgeFails(t *testing.T) {
	root, data := s.fresh("bridge-fail", 0)
	before := treeOf(t, root, rootSkip...)
	s.publish("desktop-v0.13.0")
	out, err := s.install("bridge-fail", "DAEDALUS_UPGRADE_YES=1", "DAEDALUS_UPGRADE_FIXTURE=fail")
	if err == nil || !strings.Contains(out, "Data restored and checked against the backup") {
		t.Fatalf("%v\n%s", err, out)
	}
	if after := treeOf(t, root, rootSkip...); after != before {
		t.Fatalf("the installation is not as it was:\n%s\n---\n%s", before, after)
	}
	if j := journalOf(data); j.Stage != stageRolledBack {
		t.Fatalf("journal %+v", j)
	}
}

// Two upgrades of one installation, started together as two processes. The first is held in its
// finish (the fixture waits), so the second certainly overlaps it. The second must refuse at once,
// without touching the first one's staging or the installation; the first must then finish as if
// the second had never run; and stop, --rollback and update from other terminals during it must
// answer at once (the lock is never waited on) and change nothing.
func (s *smoke) twoUpgradesAtOnce(t *testing.T) {
	root, data := s.fresh("parallel", 0) // v0.13.0: the newest published
	if v := s.version(root); v != "desktop-v0.13.0" {
		t.Fatalf("installed %s", v)
	}
	s.publish("desktop-v0.14.0")
	defer s.unpublish("desktop-v0.14.0")
	first, firstOut := s.start(root, []string{"DAEDALUS_UPGRADE_FIXTURE=ok", "DAEDALUS_UPGRADE_FIXTURE_DELAY=6"}, "upgrade", "--yes", "--data", data)
	smokeWaitFor(t, "the first upgrade to reach its finish", 30*time.Second, func() bool { return journalOf(data).Stage == stageFinishing })
	j := journalOf(data)
	staging := j.Work
	stagingBefore := treeOf(t, staging)
	dataBefore := treeOf(t, data, dataSkip...)

	// The second upgrade — the same binary the first put in place — and three other commands.
	for _, c := range []struct {
		name string
		args []string
		want string
	}{
		{"a second upgrade", []string{"upgrade", "--yes", "--data", data}, "the installation is locked"},
		{"stop", []string{"stop", "--data", data}, "the installation is locked"},
		{"--rollback", []string{"upgrade", "--rollback", "--data", data}, "the installation is locked"},
		{"update", []string{"update", "--data", data}, "the installation is locked"},
	} {
		began := time.Now()
		out, err := s.launcher(root, []string{"DAEDALUS_UPGRADE_FIXTURE=ok"}, c.args...)
		took := time.Since(began)
		if err == nil || !strings.Contains(out, c.want) {
			t.Fatalf("%s during the upgrade: %v\n%s", c.name, err, out)
		}
		if took > 3*time.Second {
			t.Fatalf("%s waited %s for the lock instead of refusing", c.name, took)
		}
		t.Logf("%s refused in %s: %s", c.name, took.Round(time.Millisecond), tail(out, 1))
	}
	if journalOf(data).Stage != stageFinishing {
		t.Fatal("the refused commands changed the journal")
	}
	if got := treeOf(t, staging); got != stagingBefore {
		t.Fatalf("the first upgrade's staging changed under it:\n%s\n---\n%s", stagingBefore, got)
	}
	if got := treeOf(t, data, dataSkip...); got != dataBefore {
		t.Fatal("the refused commands changed the data")
	}
	if err := first.Wait(); err != nil {
		t.Fatalf("the first upgrade failed: %v\n%s", err, firstOut)
	}
	if v := s.version(root); v != "desktop-v0.14.0" {
		t.Fatalf("launcher %s\n%s", v, firstOut)
	}
	j = journalOf(data)
	if j.Stage != stageCommitted {
		t.Fatalf("journal %+v", j)
	}
	if _, err := VerifyBackup(j.Backup); err != nil {
		t.Fatal(err)
	}
	entries, _ := os.ReadDir(filepath.Join(data, "backups"))
	if len(entries) != 1 {
		t.Fatalf("%d backups: the refused upgrade took one", len(entries))
	}
}

// The new launcher is killed with SIGKILL — both processes, the whole group — while the migration
// runs. The lock goes with them; the journal stays at "finishing"; everything but upgrade refuses;
// --rollback puts the launcher and the data back.
func (s *smoke) killedWhileMigrating(t *testing.T) {
	root, data := s.fresh("killed", 0)
	before := treeOf(t, root, rootSkip...)
	s.publish("desktop-v0.14.0")
	defer s.unpublish("desktop-v0.14.0")
	cmd, _ := s.start(root, []string{"DAEDALUS_UPGRADE_FIXTURE=ok", "DAEDALUS_UPGRADE_FIXTURE_DELAY=30"}, "upgrade", "--yes", "--data", data)
	smokeWaitFor(t, "the finish", 30*time.Second, func() bool { return journalOf(data).Stage == stageFinishing })
	time.Sleep(300 * time.Millisecond)
	syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
	cmd.Wait()
	if out, err := s.launcher(root, nil, "stop", "--data", data); err == nil || !strings.Contains(out, "did not finish") {
		t.Fatalf("stop after the crash: %v\n%s", err, out)
	}
	out, err := s.launcher(root, nil, "upgrade", "--rollback", "--data", data)
	if err != nil || !strings.Contains(out, "Rolled back to desktop-v0.13.0") {
		t.Fatalf("--rollback: %v\n%s", err, out)
	}
	if after := treeOf(t, root, rootSkip...); after != before {
		t.Fatalf("not as it was:\n%s\n---\n%s", before, after)
	}
}

// Ctrl+C in the terminal: SIGINT to the whole foreground group, both launchers. The new one rolls
// back; the old one keeps waiting for it instead of killing it; the result is a clean rollback.
func (s *smoke) interruptedWhileMigrating(t *testing.T) {
	root, data := s.fresh("ctrl-c", 0)
	before := treeOf(t, root, rootSkip...)
	s.publish("desktop-v0.14.0")
	defer s.unpublish("desktop-v0.14.0")
	cmd, out := s.start(root, []string{"DAEDALUS_UPGRADE_FIXTURE=ok", "DAEDALUS_UPGRADE_FIXTURE_DELAY=30"}, "upgrade", "--yes", "--data", data)
	smokeWaitFor(t, "the finish", 30*time.Second, func() bool { return journalOf(data).Stage == stageFinishing })
	time.Sleep(300 * time.Millisecond)
	syscall.Kill(-cmd.Process.Pid, syscall.SIGINT)
	cmd.Wait()
	if j := journalOf(data); j.Stage != stageRolledBack {
		t.Fatalf("journal %+v\n%s", j, out)
	}
	if after := treeOf(t, root, rootSkip...); after != before {
		t.Fatalf("not as it was:\n%s", out)
	}
	t.Logf("what the terminal showed:\n%s", tail(out.String(), 8))
}

func tail(text string, n int) string {
	lines := strings.Split(strings.TrimSpace(text), "\n")
	if len(lines) > n {
		lines = lines[len(lines)-n:]
	}
	return strings.Join(lines, "\n")
}

// Ctrl+C — twice — while the new launcher is already restoring the data: the
// restore runs to its end, the old launcher does not start a second one over it, and there is one
// replaced-* folder, not a collision.
func (s *smoke) interruptedWhileRollingBack(t *testing.T) {
	root, data := s.fresh("ctrl-c-rollback", 150)
	before := treeOf(t, root, rootSkip...)
	s.publish("desktop-v0.14.0")
	defer s.unpublish("desktop-v0.14.0")
	cmd, out := s.start(root, []string{"DAEDALUS_UPGRADE_FIXTURE=fail"}, "upgrade", "--yes", "--data", data)
	smokeWaitFor(t, "the restore to begin", 60*time.Second, func() bool {
		entries, _ := os.ReadDir(filepath.Join(data, "upgrade"))
		for _, e := range entries {
			if strings.HasPrefix(e.Name(), "replaced-") {
				return true
			}
		}
		return false
	})
	syscall.Kill(-cmd.Process.Pid, syscall.SIGINT)
	time.Sleep(50 * time.Millisecond)
	syscall.Kill(-cmd.Process.Pid, syscall.SIGINT)
	cmd.Wait()
	if j := journalOf(data); j.Stage != stageRolledBack {
		t.Fatalf("journal %+v\n%s", j, out)
	}
	if after := treeOf(t, root, rootSkip...); after != before {
		t.Fatalf("not as it was:\n%s", out)
	}
	entries, _ := os.ReadDir(filepath.Join(data, "upgrade"))
	replaced := 0
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), "replaced-") {
			replaced++
		}
	}
	if replaced != 1 {
		t.Fatalf("%d replaced-* folders", replaced)
	}
	t.Logf("what the terminal showed:\n%s", tail(out.String(), 8))
}

func (s *smoke) update(t *testing.T) {
	root, data := s.fresh("update", 0)
	before := treeOf(t, data, dataSkip...)
	if out, err := s.launcher(root, []string{"DAEDALUS_UPGRADE_FIXTURE=fail"}, "update", "--data", data); err == nil {
		t.Fatalf("a failed update exited 0\n%s", out)
	}
	if after := treeOf(t, data, dataSkip...); after != before {
		t.Fatal("a failed update did not put the data back")
	}
	if j := journalOf(data); j.Kind != kindUpdate || j.Stage != stageRolledBack {
		t.Fatalf("journal %+v", j)
	}
	if out, err := s.launcher(root, []string{"DAEDALUS_UPGRADE_FIXTURE=ok"}, "update", "--data", data); err != nil {
		t.Fatalf("%v\n%s", err, out)
	}
	j := journalOf(data)
	if j.Stage != stageCommitted {
		t.Fatalf("journal %+v", j)
	}
	if _, err := VerifyBackup(j.Backup); err != nil {
		t.Fatal(err)
	}
}

func (s *smoke) installerHandsOver(t *testing.T) {
	root, _ := s.fresh("handover", 0)
	before := treeOf(t, root)
	s.publish("desktop-v0.14.0")
	defer s.unpublish("desktop-v0.14.0")
	out, err := s.install("handover")
	if err == nil || !strings.Contains(out, "handing over to its launcher's upgrade") {
		t.Fatalf("%v\n%s", err, out)
	}
	if after := treeOf(t, root); after != before {
		t.Fatal("install.sh changed the installation itself")
	}
}

// The old launcher dies.

// consistent checks that the journal and the installation agree: committed means the new launcher
// and the migrated data, rolled-back means the old launcher and the data as before.
func (s *smoke) consistent(t *testing.T, root, data, before string) Journal {
	t.Helper()
	j := journalOf(data)
	body, _ := os.ReadFile(filepath.Join(data, "state", "daedalus.sqlite"))
	switch j.Stage {
	case stageCommitted:
		if v := s.version(root); v != j.To || string(body) != "schema migrated by "+j.To+"\n" {
			t.Fatalf("journal committed, but the launcher is %s and the data %q", v, body)
		}
		if _, err := VerifyBackup(j.Backup); err != nil {
			t.Fatal(err)
		}
	case stageRolledBack:
		if after := treeOf(t, root, rootSkip...); after != before {
			t.Fatal("journal rolled-back, but the installation is not as it was")
		}
	default:
		t.Fatalf("journal left at %q", j.Stage)
	}
	return j
}

// childPID is the --finish process: the holder the finish lock names once it has taken it over.
func finishPID(data string) int {
	holder, ok := readHolderAt(filepath.Join(data, "upgrade", finishLockName))
	if !ok {
		return 0
	}
	return holder.PID
}

func (s *smoke) refusedWhileFinishing(t *testing.T, root, data string) {
	t.Helper()
	for _, args := range [][]string{{"upgrade", "--rollback", "--data", data}, {"stop", "--data", data}, {"update", "--data", data}} {
		began := time.Now()
		out, err := s.launcher(root, nil, args...)
		if err == nil || !strings.Contains(out, "is finishing") && !strings.Contains(out, "in progress") {
			t.Fatalf("%v while --finish runs without its parent: %v\n%s", args[:1], err, out)
		}
		if time.Since(began) > 3*time.Second {
			t.Fatalf("%v waited instead of refusing", args)
		}
	}
}

func (s *smoke) parentKilledWhileMigrating(t *testing.T) {
	root, data := s.fresh("parent-kill", 0)
	s.publish("desktop-v0.14.0")
	defer s.unpublish("desktop-v0.14.0")
	cmd, _ := s.start(root, []string{"DAEDALUS_UPGRADE_FIXTURE=ok", "DAEDALUS_UPGRADE_FIXTURE_DELAY=6"}, "upgrade", "--yes", "--data", data)
	smokeWaitFor(t, "the finish", 30*time.Second, func() bool {
		return journalOf(data).Stage == stageFinishing && finishPID(data) != cmd.Process.Pid && finishPID(data) != 0
	})
	child := finishPID(data)
	cmd.Process.Kill() // the old launcher only
	// Process.Wait, not Cmd.Wait: the latter also waits for the output pipe, which the orphaned
	// --finish still holds open.
	cmd.Process.Wait()
	if !processAlive(child) {
		t.Fatal("--finish died with its parent")
	}
	s.refusedWhileFinishing(t, root, data)
	smokeWaitFor(t, "--finish to end", 60*time.Second, func() bool { return !processAlive(child) })
	if j := s.consistent(t, root, data, ""); j.Stage != stageCommitted {
		t.Fatalf("stage %s", j.Stage)
	}
}

func (s *smoke) hangupWhileMigrating(t *testing.T) {
	root, data := s.fresh("hangup", 0)
	s.publish("desktop-v0.14.0")
	defer s.unpublish("desktop-v0.14.0")
	cmd, _ := s.start(root, []string{"DAEDALUS_UPGRADE_FIXTURE=ok", "DAEDALUS_UPGRADE_FIXTURE_DELAY=4"}, "upgrade", "--yes", "--data", data)
	smokeWaitFor(t, "the finish", 30*time.Second, func() bool { return journalOf(data).Stage == stageFinishing })
	syscall.Kill(-cmd.Process.Pid, syscall.SIGHUP) // the terminal window is closed
	s.refusedWhileFinishing(t, root, data)
	cmd.Wait()
	if j := s.consistent(t, root, data, ""); j.Stage != stageCommitted {
		t.Fatalf("stage %s", j.Stage)
	}
}

// The old launcher dies after swapping the files and before it starts the new one: nobody runs
// --finish, nothing has migrated, and the documented --rollback puts everything back.
func (s *smoke) parentKilledBeforeHandover(t *testing.T) {
	root, data := s.fresh("before-handover", 0)
	before := treeOf(t, root, rootSkip...)
	s.publish("desktop-v0.14.0")
	defer s.unpublish("desktop-v0.14.0")
	cmd, _ := s.start(root, []string{"DAEDALUS_UPGRADE_FIXTURE=ok", "DAEDALUS_UPGRADE_FIXTURE_HANDOVER_PAUSE=30"}, "upgrade", "--yes", "--data", data)
	marker := filepath.Join(s.work, "tmp", fmt.Sprintf("DAEDALUS_UPGRADE_FIXTURE_HANDOVER_PAUSE.%d", cmd.Process.Pid))
	smokeWaitFor(t, "the pause before the hand-over", 30*time.Second, func() bool { return exists(marker) })
	syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
	cmd.Wait()
	if out, err := s.launcher(root, nil, "stop", "--data", data); err == nil || !strings.Contains(out, "did not finish") {
		t.Fatalf("stop: %v\n%s", err, out)
	}
	if out, err := s.launcher(root, nil, "upgrade", "--rollback", "--data", data); err != nil || !strings.Contains(out, "Rolled back") {
		t.Fatalf("--rollback: %v\n%s", err, out)
	}
	s.consistent(t, root, data, before)
}

// The narrowest moment: the new launcher has been started — it holds the inherited lock from its
// first instruction — and has not yet looked at it when the old one dies. A rollback started then
// must be refused, and the finish must go on to a consistent end.
func (s *smoke) parentKilledAfterHandover(t *testing.T) {
	root, data := s.fresh("after-handover", 0)
	s.publish("desktop-v0.14.0")
	defer s.unpublish("desktop-v0.14.0")
	cmd, _ := s.start(root, []string{"DAEDALUS_UPGRADE_FIXTURE=ok", "DAEDALUS_UPGRADE_FIXTURE_FINISH_PAUSE=3"}, "upgrade", "--yes", "--data", data)
	var child int
	smokeWaitFor(t, "the new launcher's pause", 30*time.Second, func() bool {
		entries, _ := os.ReadDir(filepath.Join(s.work, "tmp"))
		for _, e := range entries {
			if n, err := fmt.Sscanf(e.Name(), "DAEDALUS_UPGRADE_FIXTURE_FINISH_PAUSE.%d", &child); err == nil && n == 1 && child != cmd.Process.Pid && processAlive(child) {
				return true
			}
		}
		return false
	})
	cmd.Process.Kill()
	cmd.Process.Wait()
	s.refusedWhileFinishing(t, root, data)
	smokeWaitFor(t, "--finish to end", 60*time.Second, func() bool { return !processAlive(child) })
	if j := s.consistent(t, root, data, ""); j.Stage != stageCommitted {
		t.Fatalf("stage %s", j.Stage)
	}
}

// Timing-independence: the old launcher is killed at a random moment of the whole upgrade — before
// the backup, during it, in the swap, in the hand-over, in the migration — while a rollback is
// attempted over and over from another process from that moment on. Whatever the moment, when all
// processes are gone the journal and the installation agree; an upgrade left unresolved is finished
// off with the documented --rollback and then agrees too; and no rollback ever ran while a --finish
// was alive.
func (s *smoke) randomParentKills(t *testing.T) {
	rounds := 12
	if n, err := fmt.Sscanf(os.Getenv("SMOKE_ROUNDS"), "%d", &rounds); n != 1 || err != nil {
		rounds = 12
	}
	seed := time.Now().UnixNano()
	t.Logf("seed %d", seed)
	rng := rand.New(rand.NewSource(seed))
	s.publish("desktop-v0.14.0")
	defer s.unpublish("desktop-v0.14.0")
	outcomes := map[string]int{}
	for round := 0; round < rounds; round++ {
		s.unpublish("desktop-v0.14.0")
		root, data := s.fresh(fmt.Sprintf("random-%02d", round), 0)
		s.publish("desktop-v0.14.0")
		before := treeOf(t, root, rootSkip...)
		cmd, _ := s.start(root, []string{"DAEDALUS_UPGRADE_FIXTURE=ok", "DAEDALUS_UPGRADE_FIXTURE_DELAY=1"}, "upgrade", "--yes", "--data", data)
		time.Sleep(time.Duration(rng.Intn(1600)) * time.Millisecond)
		cmd.Process.Kill()
		cmd.Process.Wait()
		// Rollbacks race whatever is left, until nothing is left.
		ranWhileFinishing := false
		deadline := time.Now().Add(30 * time.Second)
		for time.Now().Before(deadline) {
			child := finishPID(data)
			finishing := child != 0 && child != cmd.Process.Pid && processAlive(child)
			out, err := s.launcher(root, nil, "upgrade", "--rollback", "--data", data)
			if err != nil && strings.Contains(out, "Rolled back") && finishing && processAlive(child) {
				ranWhileFinishing = true
			}
			if !finishing {
				break
			}
			time.Sleep(50 * time.Millisecond)
		}
		if ranWhileFinishing {
			t.Fatalf("round %d: a rollback ran while --finish was alive", round)
		}
		j := journalOf(data)
		if j.Stage != "" && j.Stage != stageCommitted && j.Stage != stageRolledBack && j.Stage != stagePrepared {
			if out, err := s.launcher(root, nil, "upgrade", "--rollback", "--data", data); err != nil || !strings.Contains(out, "Rolled back") {
				t.Fatalf("round %d: the documented --rollback failed at %s: %v\n%s", round, j.Stage, err, out)
			}
			j = journalOf(data)
		}
		switch j.Stage {
		case "", stagePrepared:
			if after := treeOf(t, root, rootSkip...); after != before {
				t.Fatalf("round %d: killed before anything changed, yet the installation changed", round)
			}
		default:
			s.consistent(t, root, data, before)
		}
		outcomes[j.Stage+"/"]++
	}
	t.Logf("outcomes over %d rounds: %v", rounds, outcomes)
}

// The stack of a --finish that died.

// stackRun gives an installation a port of its own for its fixture stack and starts an upgrade
// whose --finish brings that stack up (a real process, recorded like the launcher records its
// children) and then waits in its health check.
func (s *smoke) stackRun(t *testing.T, name string) (root, data, port string, cmd *exec.Cmd) {
	t.Helper()
	s.unpublish("desktop-v0.14.0") // installed at v0.13.0, then v0.14.0 appears
	root, data = s.fresh(name, 0)
	s.publish("desktop-v0.14.0")
	port = freePort(t)
	env, _ := os.ReadFile(filepath.Join(data, ".env"))
	os.WriteFile(filepath.Join(data, ".env"), append(env, []byte("API_PORT="+port+"\n")...), 0o600)
	cmd, _ = s.start(root, []string{"DAEDALUS_UPGRADE_FIXTURE=ok", "DAEDALUS_UPGRADE_FIXTURE_STACK_PROCESS=1", "DAEDALUS_UPGRADE_FIXTURE_DELAY=60"}, "upgrade", "--yes", "--data", data)
	smokeWaitFor(t, "the stack to come up under --finish", 30*time.Second, func() bool {
		return journalOf(data).Stage == stageFinishing && portAnswers(port)
	})
	return root, data, port, cmd
}

func stackPID(data string) int {
	var record ChildRecord
	p, _ := NewPaths(data)
	body, err := os.ReadFile(filepath.Join(pidsDir(p), "supervisor.json"))
	if err != nil || json.Unmarshal(body, &record) != nil {
		return 0
	}
	return record.PID
}

// fdsOnFinishLock lists the descriptors a process holds on the finish lock.
func fdsOnFinishLock(pid int, data string) []string {
	var found []string
	entries, _ := os.ReadDir(fmt.Sprintf("/proc/%d/fd", pid))
	for _, e := range entries {
		if target, err := os.Readlink(fmt.Sprintf("/proc/%d/fd/%s", pid, e.Name())); err == nil && target == filepath.Join(data, "upgrade", finishLockName) {
			found = append(found, e.Name())
		}
	}
	return found
}

// quietAfter checks that nothing writes into the data folder any more: the stack's write log does
// not reappear in the restored data.
func quietAfter(t *testing.T, data string) {
	t.Helper()
	time.Sleep(700 * time.Millisecond)
	if exists(filepath.Join(data, "state", "stack-writes.log")) {
		t.Fatal("something still writes into the restored data folder")
	}
}

// --finish dies (kill -9) while the stack it brought up runs; the old launcher, alive, rolls back.
// The rollback must stop that stack — confirmed by its record — before it restores a byte.
func (s *smoke) finishKilledStackRunning(t *testing.T) {
	defer s.unpublish("desktop-v0.14.0")
	root, data, port, cmd := s.stackRun(t, "n7a")
	before := "" // compared below through consistent()
	_ = before
	stack, finish := stackPID(data), finishPID(data)
	if stack == 0 || finish == 0 || finish == cmd.Process.Pid {
		t.Fatalf("stack %d finish %d", stack, finish)
	}
	if fds := fdsOnFinishLock(stack, data); len(fds) != 0 {
		t.Fatalf("the stack holds the finish lock on fds %v", fds)
	}
	syscall.Kill(finish, syscall.SIGKILL)
	cmd.Process.Wait()
	if processAlive(stack) || portAnswers(port) {
		t.Fatal("the rollback left the dead --finish's stack running")
	}
	j := journalOf(data)
	if j.Stage != stageRolledBack {
		t.Fatalf("journal %+v", j)
	}
	if v := s.version(root); v != "desktop-v0.13.0" {
		t.Fatalf("launcher %s", v)
	}
	body, _ := os.ReadFile(filepath.Join(data, "state", "daedalus.sqlite"))
	if string(body) != "schema v1\n" {
		t.Fatalf("data %q", body)
	}
	quietAfter(t, data)
}

// Both launchers die while the stack runs. The stack must not hold the installation lock:
// the documented --rollback takes it, stops the recorded stack, and restores.
func (s *smoke) bothKilledStackRunning(t *testing.T) {
	defer s.unpublish("desktop-v0.14.0")
	root, data, port, cmd := s.stackRun(t, "n7b")
	stack, finish := stackPID(data), finishPID(data)
	syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL) // both launchers; the stack is in a group of its own
	cmd.Process.Wait()
	smokeWaitFor(t, "--finish to be gone", 10*time.Second, func() bool { return !processAlive(finish) })
	if !processAlive(stack) || !portAnswers(port) {
		t.Fatal("the stack did not outlive the launchers; the scenario did not happen")
	}
	if fds := fdsOnFinishLock(stack, data); len(fds) != 0 {
		t.Fatalf("the stack holds the finish lock on fds %v", fds)
	}
	if out, err := s.launcher(root, nil, "stop", "--data", data); err == nil || !strings.Contains(out, "did not finish") || strings.Contains(out, "locked") {
		t.Fatalf("with both launchers gone the installation is not merely unfinished: %v\n%s", err, out)
	}
	out, err := s.launcher(root, nil, "upgrade", "--rollback", "--data", data)
	if err != nil || !strings.Contains(out, "Rolled back to desktop-v0.13.0") {
		t.Fatalf("--rollback: %v\n%s", err, out)
	}
	if processAlive(stack) || portAnswers(port) {
		t.Fatal("--rollback restored the data with the stack still running")
	}
	body, _ := os.ReadFile(filepath.Join(data, "state", "daedalus.sqlite"))
	if string(body) != "schema v1\n" || s.version(root) != "desktop-v0.13.0" {
		t.Fatalf("not put back: %q", body)
	}
	quietAfter(t, data)
}

// The stack runs but its record is gone, so nothing confirms it is this installation's. The
// rollback must not restore under it: it stops with rollback-failed and the data as it is. Once the
// process is stopped by hand, the documented --rollback finishes the job.
func (s *smoke) unconfirmedStackRefusesRestore(t *testing.T) {
	defer s.unpublish("desktop-v0.14.0")
	root, data, port, cmd := s.stackRun(t, "n7a-unconfirmed")
	stack, finish := stackPID(data), finishPID(data)
	t.Cleanup(func() { killPID(stack) })
	if p, err := NewPaths(data); err == nil {
		os.Remove(filepath.Join(pidsDir(p), "supervisor.json"))
	}
	syscall.Kill(finish, syscall.SIGKILL)
	cmd.Process.Wait()
	j := journalOf(data)
	if j.Stage != stageRollbackFailed || !strings.Contains(j.Error, "stopping the stack before restoring the data") {
		t.Fatalf("journal %+v", j)
	}
	body, _ := os.ReadFile(filepath.Join(data, "state", "daedalus.sqlite"))
	if string(body) != "schema migrated by desktop-v0.14.0\n" {
		t.Fatalf("the rollback touched the data under a running stack: %q", body)
	}
	if entries, _ := filepath.Glob(filepath.Join(data, "upgrade", "replaced-*")); len(entries) != 0 {
		t.Fatalf("data was moved aside under a running stack: %v", entries)
	}
	// By hand, as the message says: stop the process, then the rollback again.
	killPID(stack)
	smokeWaitFor(t, "the stack to stop", 10*time.Second, func() bool { return !portAnswers(port) })
	out, err := s.launcher(root, nil, "upgrade", "--rollback", "--data", data)
	if err != nil || !strings.Contains(out, "Rolled back to desktop-v0.13.0") {
		t.Fatalf("--rollback: %v\n%s", err, out)
	}
	body, _ = os.ReadFile(filepath.Join(data, "state", "daedalus.sqlite"))
	if string(body) != "schema v1\n" {
		t.Fatalf("data %q", body)
	}
}
