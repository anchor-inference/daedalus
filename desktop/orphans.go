package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// Native children run in process groups of their own (native_unix.go, native_windows.go), so a
// launcher that is killed — OOM, a closed session, `kill -9` — leaves them running with nothing
// above them. Each child therefore leaves a record while it runs: its pid and the program it was
// started as, under <local>/pids. A record whose process is gone is stale and is dropped; a
// record whose process is alive *and* is still that program is an orphan of this installation, and
// is stopped before anything starts the stack again, backs it up or upgrades it.
//
// What this does not see: the bot, which the supervisor starts in a session of its own. Stopping an
// orphaned supervisor drains its bot the same way a normal stop does; a bot orphaned by a supervisor
// that died first is found only by the app's port still answering, which the stop checks after.

// ChildRecord is one running child.
type ChildRecord struct {
	Name    string    `json:"name"`
	PID     int       `json:"pid"`
	Program string    `json:"program"`
	Started time.Time `json:"started"`
	// StartMarker is the operating system's own record of when that pid started. A pid is reused;
	// the pair is not. A record without it is never acted on.
	StartMarker string `json:"start_marker"`
	// Only a child started by the Windows launcher with CREATE_NEW_CONSOLE may receive
	// group-0 CTRL_BREAK; older records can share the operator's console.
	IsolatedConsole bool `json:"isolated_console,omitempty"`
}

func pidsDir(p Paths) string { return filepath.Join(p.Local, "pids") }

func writeChildRecord(file string, record ChildRecord) {
	if file == "" {
		return
	}
	if marker, ok := processStartMarker(record.PID); ok {
		record.StartMarker = marker
	}
	if err := os.MkdirAll(filepath.Dir(file), 0o700); err != nil {
		return
	}
	body, _ := json.Marshal(record)
	_ = os.WriteFile(file, body, 0o600)
}

func removeChildRecord(file string, pid int) {
	if file == "" {
		return
	}
	// Only this child's record: a restart may already have written the next one's.
	var record ChildRecord
	if body, err := os.ReadFile(file); err == nil && json.Unmarshal(body, &record) == nil && record.PID == pid {
		_ = os.Remove(file)
	}
}

// childProgram is the program a child's record names: the one the process is once it runs. A child
// started through systemd-run's scope (the browser daemon, for its memory cap) is systemd-run only
// until it execs the command after "--"; a record naming systemd-run matched no running process,
// so FindOrphans dropped it as someone else's, and a browser daemon left by a crashed launcher kept
// its working directory in the data folder and refused every rollback until it was killed by hand.
func childProgram(argv []string) string {
	if len(argv) == 0 {
		return ""
	}
	if name := strings.TrimSuffix(filepath.Base(argv[0]), ".exe"); name == "systemd-run" {
		for i, arg := range argv {
			if arg == "--" && i+1 < len(argv) {
				return argv[i+1]
			}
		}
	}
	return argv[0]
}

// Orphan is a recorded child still running, or one whose program cannot be confirmed.
type Orphan struct {
	ChildRecord
	file      string
	confirmed bool
}

// FindOrphans reads the records. A dead process's record is removed on the way.
func FindOrphans(p Paths) []Orphan {
	entries, err := os.ReadDir(pidsDir(p))
	if err != nil {
		return nil
	}
	var out []Orphan
	for _, entry := range entries {
		file := filepath.Join(pidsDir(p), entry.Name())
		var record ChildRecord
		body, err := os.ReadFile(file)
		if err != nil || json.Unmarshal(body, &record) != nil || record.PID <= 0 {
			_ = os.Remove(file)
			continue
		}
		if !processAlive(record.PID) {
			_ = os.Remove(file)
			continue
		}
		// Identity, not just a live pid: the same start time and the same program. A pid that now
		// names another process is someone else's, and is left alone.
		marker, markerOK := processStartMarker(record.PID)
		line, lineOK := commandLineOf(record.PID)
		if (markerOK && record.StartMarker != "" && marker != record.StartMarker) || (lineOK && !strings.Contains(line, record.Program)) {
			_ = os.Remove(file)
			continue
		}
		confirmed := markerOK && lineOK && record.StartMarker != "" && marker == record.StartMarker
		out = append(out, Orphan{ChildRecord: record, file: file, confirmed: confirmed})
	}
	return out
}

// StopOrphans ends every orphan this installation's records confirm — same pid, same start time,
// same program — the whole group each, and waits for them to be gone. One it cannot confirm is not
// touched and makes this fail: a process that might be someone else's is not killed on a guess.
// Between the check and the signal a pid cannot be reused, because the process it names is alive.
func StopOrphans(ctx context.Context, p Paths, log func(string, ...any)) error {
	orphans := FindOrphans(p)
	var unconfirmed []string
	for _, orphan := range orphans {
		if !orphan.confirmed {
			unconfirmed = append(unconfirmed, fmt.Sprintf("%s (pid %d)", orphan.Name, orphan.PID))
			continue
		}
		log("stopping %s (pid %d), left running by a launcher that did not stop it", orphan.Name, orphan.PID)
		terminatePID(orphan.PID, orphan.IsolatedConsole)
	}
	deadline := time.Now().Add(40 * time.Second)
	for _, orphan := range orphans {
		if !orphan.confirmed {
			continue
		}
		for processAlive(orphan.PID) && time.Now().Before(deadline) && ctx.Err() == nil {
			time.Sleep(100 * time.Millisecond)
		}
		if processAlive(orphan.PID) {
			log("%s (pid %d) did not stop in time; killing it", orphan.Name, orphan.PID)
			killPID(orphan.PID)
			for i := 0; i < 50 && processAlive(orphan.PID); i++ {
				time.Sleep(100 * time.Millisecond)
			}
		}
		if processAlive(orphan.PID) {
			return fmt.Errorf("%s (pid %d) is still running and could not be stopped", orphan.Name, orphan.PID)
		}
		_ = os.Remove(orphan.file)
	}
	if len(unconfirmed) > 0 {
		return fmt.Errorf("processes recorded as this installation's are still running and could not be confirmed as its own: %s. "+
			"Stop them yourself, or remove their records in %s if they are not Daedalus", strings.Join(unconfirmed, ", "), pidsDir(p))
	}
	return nil
}
