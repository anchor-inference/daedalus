package main

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// The installation lock. One process at a time may run the stack of an installation or change it:
// the launcher for as long as it is open, an upgrade from its first question to its commit or
// rollback, an update, a rollback. It is the operating system's own lock on <data>/.lock — flock on
// macOS and Linux, a file opened with no sharing on Windows — so it goes away with the process that
// held it, whatever way that process ended, and there is no stale lock to clean up by hand.
//
// The file's content says who holds it, for the message the next one sees, and carries a token: an
// upgrade's --finish runs in a second process (the new launcher) while the first still holds the
// lock, and finds the same token in the journal. That is how it knows the lock it cannot take is
// the upgrade's own and not someone else's.

const lockName = ".lock"

// Holder is what the lock file says.
type Holder struct {
	Kind    string    `json:"kind"` // launcher, upgrade, update or rollback
	PID     int       `json:"pid"`
	Token   string    `json:"token"`
	Since   time.Time `json:"since"`
	Version string    `json:"version"`
}

// InstallLock is a held lock. Release gives it up; the process ending does too.
type InstallLock struct {
	file   *os.File
	holder Holder
	path   string
	// inherited marks a descriptor shared with another process (the upgrade's first launcher):
	// releasing it closes this process's copy and leaves the lock with the other one.
	inherited bool
}

// shared is the same held lock seen from a second holder in this process: releasing it does not
// release the first one's.
func (l *InstallLock) shared() *InstallLock {
	return &InstallLock{holder: l.holder, path: l.path, inherited: true}
}

var errLocked = errors.New("the installation is locked")

// The finish lock. An upgrade's last part — moving the checkouts, the migrations, the health check,
// the commit — runs in the new launcher (`upgrade --finish`), a second process. It holds a lock of
// its own, <data>/upgrade/.finish-lock, for all of that time. Whoever takes the installation lock
// also finds this one free or gives up, so an upgrade's finish excludes everything else for as long
// as its own process lives — not only for as long as the launcher that started it does. That launcher
// may die (kill -9, OOM, a closed terminal); the finish goes on, and nothing can roll back under it.
const finishLockName = ".finish-lock"

func finishLockPath(p Paths) string { return filepath.Join(upgradeDir(p), finishLockName) }

// lockPath is inside upgrade/, the launcher's own bookkeeping beside the journal: a backup skips it
// and a restore leaves it where it is, and the data folder itself gains no file of the launcher's.
// The file stays after a release — removing a flock'd file lets a process that opened it before the
// removal lock a file nobody else can see — so it is empty then.
func lockPath(p Paths) string { return filepath.Join(upgradeDir(p), lockName) }

// AcquireLock takes the installation lock without waiting. When someone else holds it, the error
// says who, and wraps errLocked.
func AcquireLock(p Paths, kind string) (*InstallLock, error) {
	lock, err := acquireAt(p, lockPath(p), kind)
	if err != nil {
		return nil, err
	}
	// An upgrade's finish still running keeps the installation busy, whoever let go of this lock.
	if holder, held := finishHeld(p); held {
		lock.Release()
		return nil, fmt.Errorf("%w: %s", errLocked, describeHolder(holder, true))
	}
	return lock, nil
}

// AcquireFinishLock takes the finish lock, for `upgrade --finish`.
func AcquireFinishLock(p Paths) (*InstallLock, error) {
	return acquireAt(p, finishLockPath(p), "finish")
}

// finishHeld reports whether some process holds the finish lock, and who.
func finishHeld(p Paths) (Holder, bool) {
	if !exists(finishLockPath(p)) {
		return Holder{}, false
	}
	file, err := lockFile(finishLockPath(p))
	if err != nil {
		holder, ok := readHolderAt(finishLockPath(p))
		if !ok {
			holder = Holder{Kind: "finish"}
		}
		return holder, true
	}
	unlockFile(file)
	return Holder{}, false
}

func acquireAt(p Paths, path, kind string) (*InstallLock, error) {
	if err := os.MkdirAll(upgradeDir(p), 0o700); err != nil {
		return nil, err
	}
	file, err := lockFile(path)
	if err != nil {
		if errors.Is(err, errLocked) {
			holder, ok := readHolderAt(path)
			return nil, fmt.Errorf("%w: %s", errLocked, describeHolder(holder, ok))
		}
		return nil, err
	}
	token := make([]byte, 16)
	if _, err := rand.Read(token); err != nil {
		unlockFile(file)
		return nil, err
	}
	holder := Holder{Kind: kind, PID: os.Getpid(), Token: hex.EncodeToString(token), Since: time.Now().UTC(), Version: version}
	body, _ := json.Marshal(holder)
	if err := file.Truncate(0); err == nil {
		_, _ = file.WriteAt(body, 0)
		_ = file.Sync()
	}
	// On Windows the open handle denies every other reader too, so the holder is also kept beside
	// it, in a file anyone may read.
	_ = os.WriteFile(path+".holder", body, 0o600)
	return &InstallLock{file: file, holder: holder, path: path}, nil
}

// Release gives the lock up.
func (l *InstallLock) Release() {
	if l == nil || l.file == nil {
		return
	}
	if l.inherited {
		// Closing this copy only; an unlock here would unlock the other holder's too.
		_ = l.file.Close()
		l.file = nil
		return
	}
	_ = os.Remove(l.path + ".holder")
	_ = l.file.Truncate(0)
	unlockFile(l.file)
	l.file = nil
}

// Token is what an upgrade writes into its journal for --finish to find.
func (l *InstallLock) Token() string {
	if l == nil {
		return ""
	}
	return l.holder.Token
}

func readHolder(p Paths) (Holder, bool) { return readHolderAt(lockPath(p)) }

func readHolderAt(path string) (Holder, bool) {
	var holder Holder
	for _, name := range []string{path + ".holder", path} {
		body, err := os.ReadFile(name)
		if err != nil || len(strings.TrimSpace(string(body))) == 0 {
			continue
		}
		if json.Unmarshal(body, &holder) == nil && holder.Kind != "" {
			return holder, true
		}
	}
	return Holder{}, false
}

func describeHolder(holder Holder, ok bool) string {
	if !ok {
		return "another process holds it"
	}
	switch holder.Kind {
	case "launcher":
		return fmt.Sprintf("a launcher is running on it (pid %d); close it first, or use its own buttons", holder.PID)
	case "upgrade":
		return fmt.Sprintf("an upgrade is in progress (pid %d, since %s); wait for it to finish", holder.PID, holder.Since.Local().Format("15:04:05"))
	case "update":
		return fmt.Sprintf("an update is in progress (pid %d, since %s); wait for it to finish", holder.PID, holder.Since.Local().Format("15:04:05"))
	case "rollback":
		return fmt.Sprintf("a rollback is in progress (pid %d); wait for it to finish", holder.PID)
	case "finish":
		if holder.PID > 0 && !processAlive(holder.PID) {
			// The named process is gone and the lock is still held: by a process it started. That
			// should not happen (the descriptor is close-on-exec); if it does, say so rather than
			// promise an ending that will not come.
			// The launcher that took the lock is gone and the lock is still held: by the --finish it
			// handed the lock to, which has not yet put its own pid here.
			return fmt.Sprintf("an upgrade is finishing: the launcher that began it (pid %d) is gone and the new launcher it handed the lock to still holds it; wait for it to end — it commits or rolls back by itself. If no `daedalus-desktop upgrade` process of this installation is left, stop its Daedalus processes by hand", holder.PID)
		}
		return fmt.Sprintf("an upgrade is finishing in the new launcher (pid %d, since %s); wait for it to end — it commits or rolls back by itself", holder.PID, holder.Since.Local().Format("15:04:05"))
	}
	return fmt.Sprintf("%s (pid %d) holds it", holder.Kind, holder.PID)
}

// LockHeldByOther reports whether someone other than `own` holds the lock right now, and who. It
// takes the lock and gives it back at once when it is free; nothing else is changed.
func LockHeldByOther(p Paths, own *InstallLock) (Holder, bool) {
	if own != nil && own.file != nil {
		return Holder{}, false
	}
	if holder, held := finishHeld(p); held {
		return holder, true
	}
	if !exists(lockPath(p)) {
		return Holder{}, false
	}
	file, err := lockFile(lockPath(p))
	if err != nil {
		holder, ok := readHolder(p)
		if !ok {
			holder = Holder{Kind: "unknown"}
		}
		return holder, true
	}
	unlockFile(file)
	return Holder{}, false
}

// readLockHolder reports who holds the installation lock right now, if anyone.
func readLockHolder(p Paths) (Holder, bool) {
	if !exists(lockPath(p)) {
		return Holder{}, false
	}
	file, err := lockFile(lockPath(p))
	if err != nil {
		holder, ok := readHolder(p)
		if !ok {
			holder = Holder{Kind: "unknown"}
		}
		return holder, true
	}
	unlockFile(file)
	return Holder{}, false
}

// rewriteHolder records the current holder in the lock file and beside it.
func (l *InstallLock) rewriteHolder() {
	body, _ := json.Marshal(l.holder)
	if l.file != nil {
		if err := l.file.Truncate(0); err == nil {
			_, _ = l.file.WriteAt(body, 0)
		}
	}
	_ = os.WriteFile(l.path+".holder", body, 0o600)
}
