package main

import (
	"context"
	"os/exec"
	"path/filepath"
	"runtime"
	"testing"
	"time"
)

func TestTheRecordOfAScopedChildNamesTheProgramItBecomes(t *testing.T) {
	scope := []string{"/usr/bin/systemd-run", "--user", "--scope", "--quiet", "--", "ignored"}
	argv := browserdArgv("/opt/daedalus/browserd", Paths{Local: "/x"}, scope[:len(scope)-1])
	if got := childProgram(argv); got != "/opt/daedalus/browserd" {
		t.Fatalf("the record names %q, not the daemon", got)
	}
	if got := childProgram([]string{"/opt/daedalus/ptyd", "serve", "--", "x"}); got != "/opt/daedalus/ptyd" {
		t.Fatalf("a child without a wrapper is recorded as %q", got)
	}
}

// A browser daemon left running by a launcher that died is found and stopped: its record must still
// match the process once systemd-run has become the daemon.
func TestAScopedOrphanIsFoundAndStopped(t *testing.T) {
	if runtime.GOOS != "linux" {
		t.Skip("systemd scopes are Linux's")
	}
	run, err := exec.LookPath("systemd-run")
	if err != nil {
		t.Skip("no systemd-run")
	}
	scope := systemScope(context.Background(), func(string) (string, error) { return run, nil }, runProbe)
	if scope == nil {
		t.Skip("no systemd user manager here")
	}
	sleep, err := exec.LookPath("sleep")
	if err != nil {
		t.Skip("no sleep")
	}
	p := fixtureData(t)
	argv := append(append([]string(nil), scope...), sleep, "60")
	cmd := exec.Command(argv[0], argv[1:]...)
	setProcessGroup(cmd)
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	// Reaped as soon as it exits, as init reaps the daemon of a launcher that is gone: a zombie
	// would still count as alive.
	reaped := make(chan struct{})
	go func() { _ = cmd.Wait(); close(reaped) }()
	defer func() { _ = cmd.Process.Kill(); <-reaped }()
	// systemd-run execs the command in the same process once the scope exists.
	deadline := time.Now().Add(10 * time.Second)
	for {
		if line, ok := commandLineOf(cmd.Process.Pid); ok && len(line) > 0 && filepath.Base(firstWord(line)) == "sleep" {
			break
		}
		if time.Now().After(deadline) {
			t.Skip("systemd-run did not become the command in time")
		}
		time.Sleep(50 * time.Millisecond)
	}
	file := filepath.Join(pidsDir(p), "browserd.json")
	writeChildRecord(file, ChildRecord{Name: "browser daemon", PID: cmd.Process.Pid, Program: childProgram(argv), Started: time.Now().UTC()})
	orphans := FindOrphans(p)
	if len(orphans) != 1 || !orphans[0].confirmed {
		t.Fatalf("the scoped daemon is not a confirmed orphan: %+v (record kept: %v)", orphans, exists(file))
	}
	if err := StopOrphans(context.Background(), p, t.Logf); err != nil {
		t.Fatal(err)
	}
	<-reaped
	if processAlive(cmd.Process.Pid) {
		t.Fatal("the orphan is still running")
	}
}

func firstWord(line string) string {
	for i, r := range line {
		if r == ' ' || r == 0 {
			return line[:i]
		}
	}
	return line
}

// A Start made while this launcher's own children are running — the page's button after a start
// that timed out — must not take them for a dead launcher's and stop them: the loops keeping them
// alive started them straight again, and the supervisor's bot was cut off mid-boot.
func TestAChildThisLauncherRunsIsNotAnOrphan(t *testing.T) {
	argv := []string{"sh", "-c", "sleep 60"}
	if runtime.GOOS == "windows" {
		argv = []string{"ping", "-n", "60", "127.0.0.1"}
	}
	p := fixtureData(t)
	record := filepath.Join(pidsDir(p), "supervisor.json")
	pr := &Process{
		Name:    "fake supervisor",
		Argv:    argv,
		LogPath: filepath.Join(t.TempDir(), "child.log"),
		Log:     func(string, ...any) {},
		PidFile: record,
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	pr.Start(ctx)
	defer func() {
		// ping answers a console break with its statistics and carries on, so the polite stop
		// would wait out its forty seconds: the child is killed once the loop has been told to stop.
		pr.mu.Lock()
		cmd := pr.cmd
		pr.mu.Unlock()
		quick, done := context.WithTimeout(ctx, time.Second)
		defer done()
		pr.Stop(quick)
		killGroup(cmd)
	}()
	deadline := time.Now().Add(10 * time.Second)
	for !exists(record) && time.Now().Before(deadline) {
		time.Sleep(20 * time.Millisecond)
	}
	if !exists(record) {
		t.Fatal("the child left no record")
	}
	if orphans := FindOrphans(p); len(orphans) != 0 {
		t.Fatalf("this launcher's own child was taken for an orphan: %+v", orphans)
	}
	if err := StopOrphans(ctx, p, t.Logf); err != nil {
		t.Fatal(err)
	}
	if !pr.Running() || pr.Starts() != 1 || !exists(record) {
		t.Fatalf("the child was disturbed: running=%v starts=%d record=%v", pr.Running(), pr.Starts(), exists(record))
	}
}
