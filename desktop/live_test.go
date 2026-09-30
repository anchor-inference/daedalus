package main

// Tests on live processes: the installation lock held by another process and released by its
// death, a stack a "crashed launcher" left running and the orphan stop that ends it, pid reuse, an
// answer on the app's port from something that is not this start's stack, and two upgrades at once.
// The processes are this test binary re-run as a helper (TestMain) or `sleep`, all on loopback and
// in temporary folders; nothing else on the machine is looked at or touched.

import (
	"bufio"
	"bytes"
	"context"
	"errors"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"
)

const helperEnv = "DAEDALUS_TEST_HELPER"

func TestMain(m *testing.M) {
	switch os.Getenv(helperEnv) {
	case "hold-lock":
		// An upgrade (or a launcher) holding the installation lock until it is killed.
		p, _ := NewPaths(os.Getenv("HELPER_DATA"))
		lock, err := AcquireLock(p, os.Getenv("HELPER_KIND"))
		if err != nil {
			fmt.Println("error", err)
			os.Exit(3)
		}
		fmt.Println("locked", lock.Token())
		time.Sleep(time.Minute)
		os.Exit(0)
	case "hold-finish":
		// An upgrade's --finish (or the launcher that began it) holding the finish lock.
		p, _ := NewPaths(os.Getenv("HELPER_DATA"))
		lock, err := AcquireFinishLock(p)
		if err != nil {
			fmt.Println("error", err)
			os.Exit(3)
		}
		fmt.Println("locked", lock.Token())
		time.Sleep(time.Minute)
		os.Exit(0)
	case "serve":
		// A stack answering /app, with the boot id it was given (or none).
		listener, err := net.Listen("tcp", "127.0.0.1:"+os.Getenv("HELPER_PORT"))
		if err != nil {
			fmt.Println("error", err)
			os.Exit(3)
		}
		fmt.Println("serving")
		boot := os.Getenv("DAEDALUS_BOOT_ID")
		_ = http.Serve(listener, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if boot != "" {
				w.Header().Set(bootHeader, boot)
			}
			w.WriteHeader(http.StatusOK)
		}))
		os.Exit(0)
	}
	os.Exit(m.Run())
}

// helperProc is a running helper. Its exit is collected by one goroutine, as init would collect a
// real orphan's; gone is closed when it has.
type helperProc struct {
	Process *os.Process
	gone    chan struct{}
}

// helper starts this test binary as a helper in a process group of its own, as the launcher starts
// a child, and waits for its first line.
func helper(t *testing.T, mode string, env ...string) (*helperProc, string) {
	t.Helper()
	exe, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command(exe, "-test.run=^$")
	cmd.Env = append(append(os.Environ(), helperEnv+"="+mode), env...)
	setProcessGroup(cmd)
	out, err := cmd.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	line, _ := bufio.NewReader(out).ReadString('\n')
	proc := &helperProc{Process: cmd.Process, gone: make(chan struct{})}
	go func() { _ = cmd.Wait(); close(proc.gone) }()
	t.Cleanup(func() {
		killPID(cmd.Process.Pid)
		<-proc.gone
	})
	return proc, strings.TrimSpace(line)
}

func freePort(t *testing.T) string {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer l.Close()
	return fmt.Sprint(l.Addr().(*net.TCPAddr).Port)
}

// ---- the lock --------------------------------------------------------------------------------

func TestTheLockIsExclusiveAndSaysWhoHasIt(t *testing.T) {
	p, _ := NewPaths(t.TempDir())
	first, err := AcquireLock(p, "upgrade")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := AcquireLock(p, "update"); !errors.Is(err, errLocked) || !strings.Contains(err.Error(), "an upgrade is in progress") {
		t.Fatalf("second lock: %v", err)
	}
	if err := InterruptedUpgrade(p); !errors.Is(err, errLocked) {
		t.Fatalf("a start during an upgrade is not refused: %v", err)
	}
	first.Release()
	second, err := AcquireLock(p, "launcher")
	if err != nil {
		t.Fatalf("the lock was not released: %v", err)
	}
	// A launcher holding it does not make the others' commands refuse (they hand over to it).
	if err := InterruptedUpgrade(p); err != nil {
		t.Fatalf("a running launcher blocks everything: %v", err)
	}
	second.Release()
}

func TestAKilledHolderReleasesTheLock(t *testing.T) {
	p, _ := NewPaths(t.TempDir())
	cmd, line := helper(t, "hold-lock", "HELPER_DATA="+p.Data, "HELPER_KIND=upgrade")
	if !strings.HasPrefix(line, "locked") {
		t.Fatalf("helper: %q", line)
	}
	if _, err := AcquireLock(p, "update"); !errors.Is(err, errLocked) {
		t.Fatalf("another process's lock was not seen: %v", err)
	}
	if holder, held := LockHeldByOther(p, nil); !held || holder.Kind != "upgrade" || holder.PID != cmd.Process.Pid {
		t.Fatalf("holder %+v %v", holder, held)
	}
	// The process dies as a crash would, with no chance to clean up.
	killPID(cmd.Process.Pid)
	<-cmd.gone
	lock, err := AcquireLock(p, "rollback")
	if err != nil {
		t.Fatalf("a dead holder's lock was not released by the system: %v", err)
	}
	lock.Release()
}

// ---- two at once ---------------------------------------------------------------------------------

func TestTwoUpgradesAtOnceOneRunsAndTheOtherChangesNothing(t *testing.T) {
	withVersion(t, "desktop-v0.12.0")
	in := newInstallation(t)
	newRelease().serve(t, "desktop-v0.13.0")
	first := in.upgrader("yes\n", true)
	second := in.upgrader("yes\n", true)
	// The first one pauses with its backup written, as long as it takes the second to try.
	var errSecond error
	hook := writeJournalHook
	t.Cleanup(func() { writeJournalHook = hook })
	var once sync.Once
	writeJournalHook = func(j *Journal) {
		if j.Stage == stageBackedUp {
			once.Do(func() { errSecond = second.Run(context.Background()) })
		}
	}
	if err := first.Run(context.Background()); err != nil {
		t.Fatalf("the first upgrade failed: %v\n%s", err, first.out)
	}
	if !errors.Is(errSecond, errLocked) {
		t.Fatalf("the second upgrade was not refused: %v", errSecond)
	}
	if in.file(t, launcherName()) != "new launcher" || in.file(t, "data/state/daedalus.sqlite") != "schema v2\n" {
		t.Fatal("the first upgrade did not complete")
	}
	entries, _ := os.ReadDir(backupsDir(in.paths))
	if len(entries) != 1 {
		t.Fatalf("%d backups; the refused upgrade must not have taken one", len(entries))
	}
}

func TestUpdateFromATerminalIsRefusedWhileALauncherHoldsTheInstallation(t *testing.T) {
	u, in := updater(t)
	cmd, line := helper(t, "hold-lock", "HELPER_DATA="+in.paths.Data, "HELPER_KIND=launcher")
	if !strings.HasPrefix(line, "locked") {
		t.Fatalf("helper: %q", line)
	}
	err := u.update(context.Background())
	if !errors.Is(err, errLocked) || !strings.Contains(err.Error(), "a launcher is running") {
		t.Fatalf("err = %v", err)
	}
	if in.stack.stops != 0 || in.stack.updates != 0 || exists(backupsDir(in.paths)) {
		t.Fatal("something was done")
	}
	_ = cmd
}

// ---- health ------------------------------------------------------------------------------------

func TestHealthAcceptsOnlyThisStartsStack(t *testing.T) {
	port := freePort(t)
	_, line := helper(t, "serve", "HELPER_PORT="+port, "DAEDALUS_BOOT_ID=ours")
	if line != "serving" {
		t.Fatalf("helper: %q", line)
	}
	expectBoot(port, bootExpectation{id: "ours"})
	if err := waitReady(context.Background(), port, 3*time.Second, 50*time.Millisecond); err != nil {
		t.Fatalf("our own stack was not accepted: %v", err)
	}
	expectBoot(port, bootExpectation{id: "the-next-start", legacy: true, portWasFree: true, alive: func() bool { return true }})
	if err := waitReady(context.Background(), port, time.Second, 50*time.Millisecond); err == nil || !strings.Contains(err.Error(), "another boot id") {
		t.Fatalf("a stack with another boot id was accepted: %v", err)
	}
}

// The race: the port was free when the start checked, our supervisor is
// alive, and something else took the port and answers 200 without any boot id. For an upgrade's or
// an update's commit that is a failure; only an ordinary start (legacy) takes it.
func TestAForeignAnswerAfterTheCheckDoesNotCommit(t *testing.T) {
	port := freePort(t)
	portWasFree := !portAnswers(port)
	_, line := helper(t, "serve", "HELPER_PORT="+port) // the competitor, no boot id
	if line != "serving" || !portWasFree {
		t.Fatalf("helper: %q, free before: %v", line, portWasFree)
	}
	ourSupervisorAlive := func() bool { return true }
	expectBoot(port, bootExpectation{id: "ours", portWasFree: portWasFree, alive: ourSupervisorAlive})
	if err := waitReady(context.Background(), port, time.Second, 50*time.Millisecond); err == nil {
		t.Fatal("a strict health check accepted an answer without this start's boot id")
	}
	expectBoot(port, bootExpectation{id: "ours", legacy: true, portWasFree: portWasFree, alive: ourSupervisorAlive})
	if err := waitReady(context.Background(), port, time.Second, 50*time.Millisecond); err != nil {
		t.Fatalf("an ordinary start no longer takes an older app's answer: %v", err)
	}
}

// ---- orphans -----------------------------------------------------------------------------------

func TestAStackACrashedLauncherLeftIsFoundAndStopped(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("process groups")
	}
	p, _ := NewPaths(filepath.Join(t.TempDir(), "data"))
	port := freePort(t)
	exe, _ := os.Executable()
	cmd, line := helper(t, "serve", "HELPER_PORT="+port)
	if line != "serving" {
		t.Fatalf("helper: %q", line)
	}
	// What Process.runOnce records, left behind because the launcher died.
	writeChildRecord(filepath.Join(pidsDir(p), "supervisor.json"), ChildRecord{Name: "supervisor", PID: cmd.Process.Pid, Program: exe, Started: time.Now()})
	orphans := FindOrphans(p)
	if len(orphans) != 1 || !orphans[0].confirmed {
		t.Fatalf("orphans %+v", orphans)
	}
	// A native start refuses while the app's port is taken, and stops the orphan first.
	os.MkdirAll(p.Data, 0o755)
	os.WriteFile(p.Env, []byte("API_PORT="+port+"\n"), 0o600)
	var lines []string
	n := NewNative(p, func(format string, args ...any) { lines = append(lines, fmt.Sprintf(format, args...)) })
	_, _, err := n.clearTheWay(context.Background())
	if processAlive(cmd.Process.Pid) {
		t.Fatal("the orphan is still running")
	}
	if !strings.Contains(strings.Join(lines, "\n"), "left running by a launcher that did not stop it") {
		t.Fatalf("the stop was not said: %v", lines)
	}
	if exists(filepath.Join(pidsDir(p), "supervisor.json")) {
		t.Fatal("the record was kept")
	}
	if err != nil {
		t.Fatalf("with the orphan gone the way is not clear: %v", err)
	}
}

func TestANativeStartRefusesAPortSomethingElseHolds(t *testing.T) {
	p, _ := NewPaths(filepath.Join(t.TempDir(), "data"))
	port := freePort(t)
	_, line := helper(t, "serve", "HELPER_PORT="+port)
	if line != "serving" {
		t.Fatalf("helper: %q", line)
	}
	os.MkdirAll(p.Data, 0o755)
	os.WriteFile(p.Env, []byte("API_PORT="+port+"\n"), 0o600)
	_, _, err := NewNative(p, func(string, ...any) {}).clearTheWay(context.Background())
	if err == nil || !strings.Contains(err.Error(), "already answers") {
		t.Fatalf("err = %v", err)
	}
}

func TestAReusedPidIsNeverKilled(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("sleep")
	}
	p, _ := NewPaths(filepath.Join(t.TempDir(), "data"))
	// Someone else's process, alive, with the pid a stale record names.
	other := exec.Command("sleep", "30")
	setProcessGroup(other)
	if err := other.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { killPID(other.Process.Pid); _ = other.Wait() })
	marker, ok := processStartMarker(other.Process.Pid)
	if !ok {
		t.Skip("no start time on this system")
	}
	for name, record := range map[string]ChildRecord{
		"another start time": {Name: "supervisor", PID: other.Process.Pid, Program: "sleep", StartMarker: marker + "1"},
		"another program":    {Name: "supervisor", PID: other.Process.Pid, Program: "/data/runtime/venv/bin/python", StartMarker: marker},
		"no start time":      {Name: "supervisor", PID: other.Process.Pid, Program: "sleep"},
	} {
		file := filepath.Join(pidsDir(p), "supervisor.json")
		os.MkdirAll(pidsDir(p), 0o700)
		body := fmt.Sprintf(`{"name":%q,"pid":%d,"program":%q,"start_marker":%q}`, record.Name, record.PID, record.Program, record.StartMarker)
		os.WriteFile(file, []byte(body), 0o600)
		err := StopOrphans(context.Background(), p, func(string, ...any) {})
		if !processAlive(other.Process.Pid) {
			t.Fatalf("%s: someone else's process was killed", name)
		}
		switch name {
		case "no start time":
			// Cannot be confirmed: refused, not killed.
			if err == nil {
				t.Fatalf("%s: an unconfirmed process was let through", name)
			}
		default:
			if err != nil || exists(file) {
				t.Fatalf("%s: the stale record was not dropped (%v)", name, err)
			}
		}
		os.Remove(file)
	}
}

func TestTheStartThatDecidesAnUpgradeIsStrict(t *testing.T) {
	p, _ := NewPaths(t.TempDir())
	app := NewApp(p)
	if !app.native.expectation(true).legacy {
		t.Fatal("an ordinary start does not take an older app")
	}
	var during bootExpectation
	app.native.bootID = "x"
	// What appStack.UpdateAndCheck sets for the duration of the move and the start.
	app.native.strictHealth = true
	during = app.native.expectation(true)
	app.native.strictHealth = false
	if during.legacy {
		t.Fatal("the start after an upgrade would take an answer without a boot id")
	}
}

// Positively: an upgrade's working folder is removed by a later cleanup only once that upgrade
// has ended. One still running — or one cut off, whose old/ a rollback still needs — is left alone.
func TestCleanupTakesOnlyFinishedWorkFolders(t *testing.T) {
	dir := t.TempDir()
	running := filepath.Join(dir, "20270101T000000Z", "new")
	cutOff := filepath.Join(dir, "20270101T000001Z", "old")
	finished := filepath.Join(dir, "20270101T000002Z")
	for _, d := range []string{running, cutOff, finished} {
		os.MkdirAll(d, 0o700)
	}
	markWorkFinished(&Journal{Work: finished, Stage: stageCommitted})
	cleanOldWork(dir)
	if !exists(running) || !exists(cutOff) {
		t.Fatal("a work folder still in use was removed")
	}
	if exists(finished) {
		t.Fatal("a finished work folder was kept")
	}
}

func TestTheBridgeReadsTheTagFromAPinnedLaunchersVersion(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("sh")
	}
	script := filepath.Join(t.TempDir(), "launcher")
	os.WriteFile(script, []byte("#!/bin/sh\necho 'desktop-v1.0.0 daedalus@0123456789ab protocore-exp@ba9876543210'\n"), 0o755)
	got, err := probeVersion(context.Background(), script)
	if err != nil || got != "desktop-v1.0.0" {
		t.Fatalf("got %q, %v", got, err)
	}
}

func TestABackupTheDiskHasNoRoomForIsRefusedBeforeItStarts(t *testing.T) {
	p := fixtureData(t)
	need, err := backupRoom(p, "", nil)
	if err != nil || need < 64<<20 {
		t.Fatalf("need %d, %v", need, err)
	}
	free, err := freeBytes(p.Data)
	if err != nil || free == 0 {
		t.Fatalf("free %d, %v", free, err)
	}
	withVersion(t, "desktop-v0.12.0")
	in := newInstallation(t)
	newRelease().serve(t, "desktop-v0.13.0")
	u := in.upgrader("yes\n", true)
	u.opts.checkRoom = func(Paths, string, []string) error {
		return errors.New("the disk has 10 MB free; the backup needs about 900 MB")
	}
	if err := u.Run(context.Background()); err == nil || !strings.Contains(err.Error(), "nothing was changed") {
		t.Fatalf("err = %v", err)
	}
	if in.file(t, launcherName()) != "old launcher" || exists(backupsDir(in.paths)) {
		t.Fatal("something changed")
	}
}

// With real processes: while another process holds the finish lock, a rollback, an update, a
// start and a second --finish are all refused; when that process dies, the lock is free again.
func TestAFinishHeldByAnotherProcessExcludesEverything(t *testing.T) {
	u, in := updater(t)
	cmd, line := helper(t, "hold-finish", "HELPER_DATA="+in.paths.Data)
	token := strings.TrimPrefix(line, "locked ")
	if token == line {
		t.Fatalf("helper: %q", line)
	}
	writeJournal(in.paths, &Journal{Kind: kindUpgrade, Stage: stageSwapped, From: "desktop-v0.12.0", To: "desktop-v0.13.0", FinishToken: token})
	rollback := &Upgrader{paths: in.paths, stack: in.stack, mode: ModeNative, out: &bytes.Buffer{}, opts: upgradeOptions{rollback: true}}
	if err := rollback.Run(context.Background()); !errors.Is(err, errLocked) || !strings.Contains(err.Error(), "finishing") {
		t.Fatalf("rollback: %v", err)
	}
	if err := u.update(context.Background()); !errors.Is(err, errLocked) {
		t.Fatalf("update: %v", err)
	}
	if _, err := AcquireLock(in.paths, "launcher"); !errors.Is(err, errLocked) {
		t.Fatalf("a launcher start: %v", err)
	}
	// A second --finish, not handed the lock, in another process than its holder: refused.
	finish := &Upgrader{paths: in.paths, stack: in.stack, mode: ModeNative, out: &bytes.Buffer{}, opts: upgradeOptions{finish: true}}
	withVersion(t, "desktop-v0.13.0")
	if err := finish.Run(context.Background()); err == nil || !strings.Contains(err.Error(), "not handed one") {
		t.Fatalf("a --finish without the lock: %v", err)
	}
	if j, _ := readJournal(in.paths); j.Stage != stageSwapped || in.stack.updates != 0 {
		t.Fatal("something was done under another process's finish lock")
	}
	killPID(cmd.Process.Pid)
	<-cmd.gone
	lock, err := AcquireLock(in.paths, "rollback")
	if err != nil {
		t.Fatalf("the finish lock outlived its process: %v", err)
	}
	lock.Release()
}

// The commit checks ownership: a journal that is no longer this finish's is not written over.
func TestAFinishDoesNotCommitAJournalThatIsNoLongerItsOwn(t *testing.T) {
	withVersion(t, "desktop-v0.12.0")
	in := newInstallation(t)
	newRelease().serve(t, "desktop-v0.13.0")
	u := in.upgrader("yes\n", true)
	finish := u.opts.runNewBinary
	hook := writeJournalHook
	t.Cleanup(func() { writeJournalHook = hook })
	var tampered bool
	u.opts.runNewBinary = func(ctx context.Context, exe string, args []string) error {
		// While the migration runs, something (it cannot be a rollback any more; say a hand edit)
		// changes the journal under the finish.
		in.stack.onUpdate = func() {
			j, _ := readJournal(in.paths)
			j.FinishToken = "someone else"
			writeJournal(in.paths, j)
			tampered = true
		}
		return finish(ctx, exe, args)
	}
	u.Run(context.Background())
	if !tampered {
		t.Fatal("the migration never ran")
	}
	if j, _ := readJournal(in.paths); j.Stage == stageCommitted {
		t.Fatal("committed over a journal that was not the finish's")
	}
}
