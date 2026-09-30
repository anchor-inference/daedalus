//go:build linux

package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"
)

// The bot deliberately leaves ServiceStart processes alive on shutdown. The launcher therefore
// stops the services recorded in its database, then refuses to copy or restore data while *any*
// other process still has a cwd or descriptor in that tree. A database row alone is not authority
// to signal a PID: the process must still be the session leader born at the recorded time and in
// the recorded directory. This also prevents an old row from killing a reused PID.
type serviceWriter struct {
	PID     int     `json:"pid"`
	CWD     string  `json:"cwd"`
	Started string  `json:"started"`
	Born    float64 `json:"born"`
	Marker  string  `json:"marker"`
}

const serviceWriterQuery = `import json, os, sqlite3, sys, urllib.parse
p = sys.argv[1]
c = sqlite3.connect('file:' + urllib.parse.quote(p) + '?mode=ro', uri=True, timeout=5)
c.execute('pragma query_only=on')
try:
    rows = c.execute("select pid,cwd,started_at from services where status='running' and pid is not null").fetchall()
except sqlite3.OperationalError as e:
    if 'no such table: services' not in str(e): raise
    rows = []
btime = next(int(line.split()[1]) for line in open('/proc/stat') if line.startswith('btime '))
hz = os.sysconf('SC_CLK_TCK')
out = []
for pid,cwd,started in rows:
    try:
        stat = open('/proc/%s/stat' % pid).read().rsplit(')',1)[1].split()
        marker = stat[19]
        born = btime + int(marker)/hz
    except (OSError, ValueError, IndexError):
        marker, born = '', 0
    out.append(dict(pid=pid,cwd=cwd,started=started,born=born,marker=marker))
print(json.dumps(out))`

func recordedServiceWriters(ctx context.Context, p Paths) ([]serviceWriter, error) {
	db := filepath.Join(p.State, "daedalus.sqlite")
	f, err := os.Open(db)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var magic [16]byte
	n, readErr := f.Read(magic[:])
	f.Close()
	if readErr != nil {
		return nil, readErr
	}
	if n != len(magic) || string(magic[:]) != "SQLite format 3\x00" {
		// An older or fixture installation may have no service table. The process scan below
		// remains the final gate even when there are no records to stop automatically.
		return nil, nil
	}
	python := venvPython(p)
	if !exists(python) {
		python = "python3"
	}
	out, err := exec.CommandContext(ctx, python, "-c", serviceWriterQuery, db).CombinedOutput()
	if err != nil {
		return nil, fmt.Errorf("could not read running services from %s: %w (%s)", db, err, strings.TrimSpace(string(out)))
	}
	var rows []serviceWriter
	if err := json.Unmarshal(out, &rows); err != nil {
		return nil, fmt.Errorf("could not decode running services: %w", err)
	}
	return rows, nil
}

func quiesceData(ctx context.Context, p Paths) error {
	rows, err := recordedServiceWriters(ctx, p)
	if err != nil {
		return err
	}
	for _, row := range rows {
		if err := stopRecordedService(ctx, row, p.Data); err != nil {
			return err
		}
	}
	return checkDataWriters(p.Data)
}

func stopRecordedService(ctx context.Context, row serviceWriter, data string) error {
	if row.PID <= 1 || row.PID == os.Getpid() || !serviceAlive(row.PID) {
		return nil
	}
	if row.Marker == "" {
		return fmt.Errorf("service pid %d could not be identified; stop it manually before upgrading", row.PID)
	}
	started, err := time.Parse(time.RFC3339Nano, row.Started)
	if err != nil || row.Born == 0 || absDuration(time.Unix(0, int64(row.Born*float64(time.Second))).Sub(started)) > 3*time.Second {
		return fmt.Errorf("service pid %d has no trustworthy start time; stop it manually before upgrading", row.PID)
	}
	marker, ok := processStartMarker(row.PID)
	cwd, cwdErr := os.Readlink(fmt.Sprintf("/proc/%d/cwd", row.PID))
	pgid, pgErr := syscall.Getpgid(row.PID)
	if !ok || marker != row.Marker || cwdErr != nil || pgErr != nil || pgid != row.PID || !withinData(cwd, data) || filepath.Clean(cwd) != filepath.Clean(row.CWD) {
		return fmt.Errorf("service pid %d no longer matches its database row; stop it manually before upgrading", row.PID)
	}
	// Check the marker a second time immediately before signalling. If the process exits during
	// the check, a new PID cannot be treated as this service.
	if next, ok := processStartMarker(row.PID); !ok || next != marker {
		return fmt.Errorf("service pid %d changed while it was checked; retry the upgrade", row.PID)
	}
	if err := syscall.Kill(-row.PID, syscall.SIGTERM); err != nil && !errors.Is(err, syscall.ESRCH) {
		return fmt.Errorf("could not stop service pid %d: %w", row.PID, err)
	}
	deadline := time.Now().Add(5 * time.Second)
	for serviceAlive(row.PID) && time.Now().Before(deadline) && ctx.Err() == nil {
		time.Sleep(100 * time.Millisecond)
	}
	if serviceAlive(row.PID) {
		if err := syscall.Kill(-row.PID, syscall.SIGKILL); err != nil && !errors.Is(err, syscall.ESRCH) {
			return fmt.Errorf("could not kill service pid %d: %w", row.PID, err)
		}
		for i := 0; i < 50 && serviceAlive(row.PID); i++ {
			time.Sleep(100 * time.Millisecond)
		}
	}
	if serviceAlive(row.PID) {
		return fmt.Errorf("service pid %d is still alive; no data will be copied", row.PID)
	}
	return nil
}

func absDuration(d time.Duration) time.Duration {
	if d < 0 {
		return -d
	}
	return d
}

func serviceAlive(pid int) bool {
	body, err := os.ReadFile(fmt.Sprintf("/proc/%d/stat", pid))
	if err != nil {
		return false
	}
	parts := strings.Fields(string(body)[strings.LastIndex(string(body), ")")+1:])
	return len(parts) > 0 && parts[0] != "Z" && parts[0] != "X"
}

func withinData(path, data string) bool {
	path, data = filepath.Clean(strings.TrimSuffix(path, " (deleted)")), filepath.Clean(data)
	rel, err := filepath.Rel(data, path)
	return err == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(os.PathSeparator))
}

func uninspectableWriter(pid int, what string, err error) error {
	name, _ := os.ReadFile(fmt.Sprintf("/proc/%d/comm", pid))
	comm := strings.TrimSpace(string(name))
	if comm == "" {
		comm = "unknown process"
	}
	return fmt.Errorf("cannot inspect %s of pid %d (%s): %w; cannot prove it has no data handles. Stop this process or run the upgrade in an isolated cgroup, then retry", what, pid, comm, err)
}

// checkFolderWriters is the same look at another folder: the runtime from before the move.
func checkFolderWriters(dir string) error { return checkDataWriters(dir) }

func checkDataWriters(data string) error {
	canonical, err := filepath.EvalSymlinks(data)
	if err != nil {
		return err
	}
	procs, err := os.ReadDir("/proc")
	if err != nil {
		return err
	}
	selfCgroup, _ := os.ReadFile("/proc/self/cgroup")
	for _, proc := range procs {
		pid, err := strconv.Atoi(proc.Name())
		if err != nil || pid == os.Getpid() || !serviceAlive(pid) {
			continue
		}
		base := filepath.Join("/proc", proc.Name())
		info, err := os.Stat(base)
		if errors.Is(err, os.ErrNotExist) {
			continue
		}
		if err != nil {
			return err
		}
		if owner, ok := info.Sys().(*syscall.Stat_t); ok && owner.Uid != uint32(os.Geteuid()) {
			// A service launched by this installation runs as the installer's user. Another
			// account's protected /proc cannot be inspected by this launcher.
			continue
		}
		otherCgroup, _ := os.ReadFile(filepath.Join(base, "cgroup"))
		uninspectableOtherGroup := len(selfCgroup) > 0 && len(otherCgroup) > 0 && string(selfCgroup) != string(otherCgroup)
		if cwd, err := os.Readlink(filepath.Join(base, "cwd")); err == nil && withinData(cwd, canonical) {
			return fmt.Errorf("pid %d still has its working directory in %s", pid, canonical)
		} else if err != nil && !errors.Is(err, os.ErrNotExist) {
			if uninspectableOtherGroup && (errors.Is(err, syscall.EACCES) || errors.Is(err, syscall.EPERM)) {
				continue
			}
			return uninspectableWriter(pid, "working directory", err)
		}
		fds, err := os.ReadDir(filepath.Join(base, "fd"))
		if errors.Is(err, os.ErrNotExist) {
			continue
		}
		if err != nil {
			if uninspectableOtherGroup && (errors.Is(err, syscall.EACCES) || errors.Is(err, syscall.EPERM)) {
				continue
			}
			// Another user's /proc is often hidden; it cannot be ruled out as a writer.
			return uninspectableWriter(pid, "open files", err)
		}
		for _, fd := range fds {
			path, err := os.Readlink(filepath.Join(base, "fd", fd.Name()))
			if errors.Is(err, os.ErrNotExist) {
				continue
			}
			if err != nil {
				if uninspectableOtherGroup && (errors.Is(err, syscall.EACCES) || errors.Is(err, syscall.EPERM)) {
					break
				}
				return uninspectableWriter(pid, "fd "+fd.Name(), err)
			}
			// The parent upgrade and its --finish child intentionally share these two
			// advisory lock descriptors. Neither lock is part of the backed-up data.
			if path == filepath.Join(canonical, "upgrade", lockName) || path == filepath.Join(canonical, "upgrade", finishLockName) {
				continue
			}
			if withinData(path, canonical) {
				return fmt.Errorf("pid %d still has %s open under the data folder", pid, path)
			}
		}
	}
	return nil
}
