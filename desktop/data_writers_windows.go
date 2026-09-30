//go:build windows

package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
)

type windowsServiceWriter struct {
	PID int    `json:"pid"`
	CWD string `json:"cwd"`
}

// The service table is read only. An unreadable real database must stop the upgrade.
const windowsServiceWriterQuery = `import json, sqlite3, sys, urllib.parse
c = sqlite3.connect('file:' + urllib.parse.quote(sys.argv[1]) + '?mode=ro', uri=True, timeout=5)
c.execute('pragma query_only=on')
try:
 rows = c.execute("select pid,cwd from services where status='running' and pid is not null").fetchall()
except sqlite3.OperationalError as e:
 if 'no such table: services' not in str(e): raise
 rows = []
print(json.dumps([dict(pid=pid,cwd=cwd) for pid,cwd in rows]))`

func windowsRecordedServices(ctx context.Context, p Paths) ([]windowsServiceWriter, error) {
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
	_ = f.Close()
	if readErr != nil {
		return nil, readErr
	}
	if n != len(magic) || string(magic[:]) != "SQLite format 3\x00" {
		// Upgrade fixtures carry a plain-text state file. The file-handle gate still runs.
		return nil, nil
	}
	python := venvPython(p)
	if !exists(python) {
		python = "python"
	}
	out, err := exec.CommandContext(ctx, python, "-c", windowsServiceWriterQuery, db).CombinedOutput()
	if err != nil {
		return nil, fmt.Errorf("cannot read running services from %s: %w (%s)", db, err, strings.TrimSpace(string(out)))
	}
	var rows []windowsServiceWriter
	if err := json.Unmarshal(out, &rows); err != nil {
		return nil, fmt.Errorf("cannot decode running services: %w", err)
	}
	return rows, nil
}

func quiesceData(ctx context.Context, p Paths) error {
	rows, err := windowsRecordedServices(ctx, p)
	if err != nil {
		return err
	}
	for _, row := range rows {
		if row.PID <= 1 || row.PID == os.Getpid() {
			continue
		}
		const processQueryLimitedInformation = 0x1000
		h, err := syscall.OpenProcess(processQueryLimitedInformation, false, uint32(row.PID))
		if errors.Is(err, syscall.Errno(87)) { // ERROR_INVALID_PARAMETER: the recorded PID exited.
			continue
		}
		if err != nil {
			return fmt.Errorf("cannot establish whether recorded ServiceStart pid %d is still running: %w; stop it manually before retrying", row.PID, err)
		}
		_ = syscall.CloseHandle(h)
		// Windows has no supported way to verify another process's cwd. A stale database row
		// must not be used as authority to signal a possibly reused PID. The operator can
		// stop this service and retry; no backup or restore starts under an unknown writer.
		return fmt.Errorf("ServiceStart pid %d recorded in %s cannot be safely identified on Windows; stop that service and its children, then retry the upgrade or rollback", row.PID, row.CWD)
	}
	return checkWindowsDataWriters(p.Data)
}

func checkWindowsDataWriters(data string) error {
	root, err := filepath.Abs(data)
	if err != nil {
		return err
	}
	return filepath.WalkDir(root, func(path string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return fmt.Errorf("cannot inspect %s before backup or restore: %w", path, walkErr)
		}
		if path == root {
			return nil
		}
		rel, err := filepath.Rel(root, path)
		if err != nil {
			return err
		}
		if notBackedUp[strings.Split(rel, string(os.PathSeparator))[0]] {
			if entry.IsDir() {
				return filepath.SkipDir
			}
			return nil
		}
		if !entry.Type().IsRegular() {
			return nil
		}
		name, err := syscall.UTF16PtrFromString(path)
		if err != nil {
			return err
		}
		// A read handle which does not share WRITE refuses an existing writer, even if the
		// writer itself allows readers. A sharing violation is a hard gate before copying.
		h, err := syscall.CreateFile(name, syscall.GENERIC_READ, syscall.FILE_SHARE_READ,
			nil, syscall.OPEN_EXISTING, syscall.FILE_ATTRIBUTE_NORMAL, 0)
		if err != nil {
			return fmt.Errorf("cannot safely read %s; a data writer may still hold it (%w). Stop the writer and retry", path, err)
		}
		return syscall.CloseHandle(h)
	})
}
