//go:build upgradefixture

package main

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"time"
)

// A stack for the upgrade smoke test (desktop/upgrade-smoke.sh), compiled only into a binary built
// with -tags upgradefixture and used only when DAEDALUS_UPGRADE_FIXTURE is set and the data folder
// carries the marker file below — so a fixture binary pointed at a real installation by mistake still
// uses the real stack. Its UpdateAndCheck does what a migration does to the data — rewrites a file in
// state/ — and then, when DAEDALUS_UPGRADE_FIXTURE=fail, fails the health check.

const fixtureMarker = ".upgrade-fixture"

func init() {
	// Pauses at the two ends of the hand-over, for the smoke to kill the old launcher in them.
	pause := func(name string) func() {
		seconds, err := strconv.Atoi(os.Getenv(name))
		if err != nil || seconds <= 0 || os.Getenv("DAEDALUS_UPGRADE_FIXTURE") == "" {
			return nil
		}
		return func() {
			_ = os.WriteFile(filepath.Join(os.TempDir(), name+"."+strconv.Itoa(os.Getpid())), nil, 0o600)
			time.Sleep(time.Duration(seconds) * time.Second)
		}
	}
	handoverHook = pause("DAEDALUS_UPGRADE_FIXTURE_HANDOVER_PAUSE")
	finishStartHook = pause("DAEDALUS_UPGRADE_FIXTURE_FINISH_PAUSE")

	fixtureStack = func(p Paths, log func(string, ...any)) upgradeStack {
		if !exists(filepath.Join(p.Data, fixtureMarker)) {
			log("DAEDALUS_UPGRADE_FIXTURE is ignored: %s has no %s", p.Data, fixtureMarker)
			return appStack{NewApp(p)}
		}
		return &fixture{p: p, log: log}
	}
}

// Signal handlers installed by serveFixtureStack need every package init to have returned.
func runFixtureStack() bool {
	if len(os.Args) != 4 || os.Args[1] != "__fixture-stack" {
		return false
	}
	serveFixtureStack(os.Args[2], os.Args[3])
	return true
}

type fixture struct {
	p    Paths
	log  func(string, ...any)
	proc *Process // the stack process, when DAEDALUS_UPGRADE_FIXTURE_STACK_PROCESS=1
}

func (f *fixture) Configured() bool                                  { return true }
func (f *fixture) Snapshot(context.Context, string, *Manifest) error { return nil }
func (f *fixture) Restore(context.Context, string, *Manifest) error  { return nil }

// Stop is the real stop's shape: this process's own stack first, then what the records say another
// launcher process left running, then a quiet port.
func (f *fixture) Stop(ctx context.Context) error {
	if f.proc != nil {
		f.proc.Stop(ctx)
		f.proc = nil
	}
	return stopRecordedStack(ctx, f.p, f.log)
}

func (f *fixture) Leave(ctx context.Context) {
	if f.proc != nil {
		f.proc.Stop(ctx)
		f.proc = nil
	}
}

func (f *fixture) UpdateAndCheck(ctx context.Context) error {
	if err := os.MkdirAll(f.p.State, 0o700); err != nil {
		return err
	}
	migrated := fmt.Sprintf("schema migrated by %s\n", version)
	if err := os.WriteFile(filepath.Join(f.p.State, "daedalus.sqlite"), []byte(migrated), 0o600); err != nil {
		return err
	}
	if os.Getenv("DAEDALUS_UPGRADE_FIXTURE_STACK_PROCESS") == "1" {
		exe, _ := os.Executable()
		port := APIPort(f.p)
		f.proc = &Process{
			Name: "supervisor", Argv: []string{exe, "__fixture-stack", port, f.p.State}, Dir: f.p.Data,
			PidFile: filepath.Join(pidsDir(f.p), "supervisor.json"), Log: f.log,
		}
		f.proc.Start(ctx)
		for i := 0; i < 100 && !portAnswers(port); i++ {
			time.Sleep(50 * time.Millisecond)
		}
		if !portAnswers(port) {
			return errors.New("fixture: the stack process did not come up")
		}
	}
	// Audit knob: after the migration, wait like WaitReady does — honouring cancellation — so an
	// interruption (Ctrl+C in the terminal) can land in the middle of an upgrade's finish.
	if seconds, err := strconv.Atoi(os.Getenv("DAEDALUS_UPGRADE_FIXTURE_DELAY")); err == nil && seconds > 0 {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(time.Duration(seconds) * time.Second):
		}
	}
	if os.Getenv("DAEDALUS_UPGRADE_FIXTURE") == "fail" {
		return errors.New("fixture: the app did not answer after the migration")
	}
	return nil
}

func serveFixtureStack(port, state string) {
	go func() {
		for {
			if file, err := os.OpenFile(filepath.Join(state, "stack-writes.log"), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600); err == nil {
				fmt.Fprintf(file, "%d %s\n", os.Getpid(), time.Now().Format(time.RFC3339Nano))
				file.Close()
			}
			time.Sleep(100 * time.Millisecond)
		}
	}()
	listener, err := net.Listen("tcp", "127.0.0.1:"+port)
	if err != nil {
		os.Exit(3)
	}
	_ = http.Serve(listener, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusOK) }))
	os.Exit(0)
}
