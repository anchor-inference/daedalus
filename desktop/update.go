package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"path/filepath"
	"strings"
	"sync"
)

// safeUpdate is `update` with the same invariant as an upgrade: the checkouts are not moved, and the
// app's migrations do not run, until the data folder is protected (protect.go) — switched to a
// fenced copy with the folder from before kept whole, or backed up and read back. A start that
// fails afterwards puts the data from before back the same way. The
// journal is the upgrade's, with kind update, so a crash half-way is found on the next start the
// same way. Docker mode is refused outright until its volumes' restore is proven (UPDATES.md).
//
// The command or the button is the operator's yes; there is no second question.
func (a *App) safeUpdate(ctx context.Context) error {
	u := &Upgrader{paths: a.paths, stack: chooseStack(a), mode: a.Mode(), out: &logWriter{log: a.log}, opts: upgradeOptions{yes: true}}
	if a.lock != nil {
		// The button on the page of the launcher that runs the stack: that launcher holds the lock.
		u.lock, u.inLauncher = a.lock, true
	}
	return u.update(ctx)
}

func (u *Upgrader) update(ctx context.Context) error {
	if u.mode == ModeDocker {
		return errDockerNotCovered
	}
	if !u.inLauncher {
		// From the command line, the stack must not be running under a launcher: its processes are
		// not this process's to stop, and a backup taken under them is not a backup.
		if _, running := readInstance(u.paths); running {
			return errors.New("a launcher is running on this installation: use the Update button on its page, or close it and run update again. Nothing was changed")
		}
		release, err := u.takeLock("update")
		if err != nil {
			return err
		}
		defer release()
	}
	if err := interruptedUpgrade(u.paths, u.lock); err != nil {
		return err
	}
	if u.mode == ModeDocker {
		return errDockerNotCovered
	}
	if err := u.stack.Prepare(ctx); err != nil {
		return fmt.Errorf("the next version's environment could not be prepared, so nothing was changed: %w", err)
	}
	if err := migrateLegacyRuntime(ctx, u.paths, func(format string, args ...any) { u.say(format, args...) }); err != nil {
		return fmt.Errorf("moving the runtime out of the data folder: %w; nothing else was changed", err)
	}
	if u.stack.Configured() {
		u.say("stopping the agent before the data is protected")
		if err := u.stack.Stop(ctx); err != nil {
			return fmt.Errorf("the stack could not be stopped, so nothing was changed: %w", err)
		}
	}
	journal := &Journal{Kind: kindUpdate, From: version, To: version, Mode: string(u.mode), Stage: stagePrepared, Started: u.now().UTC()}
	if err := u.protect(ctx, journal, "", nil); err != nil {
		return err
	}
	journal.Stage = stageFinishing
	if err := writeJournal(u.paths, journal); err != nil {
		return u.rollback(ctx, journal, err)
	}
	u.say("updating the checkouts and starting the stack")
	if err := u.stack.UpdateAndCheck(ctx); err != nil {
		return u.rollback(ctx, journal, fmt.Errorf("the stack did not come up after the update: %w", err))
	}
	journal.Stage = stageCommitted
	if err := writeJournal(u.paths, journal); err != nil {
		return u.rollback(ctx, journal, err)
	}
	if !u.inLauncher {
		// From the command line there is no launcher to keep the new stack company: a native agent
		// nobody can see is one nobody can stop. The next start brings it up under a launcher.
		u.stack.Leave(ctx)
	}
	u.cleanUpAfter(journal)
	kept := journal.Backup
	if journal.Pre != "" {
		kept = filepath.Join(fenceControlPath(u.paths.Data), "retained", journal.Pre)
	}
	u.say("updated; the data from before it stays at %s", kept)
	return nil
}

// logWriter turns what an Upgrader says into the launcher's log lines, so the page shows it.
type logWriter struct {
	mu  sync.Mutex
	buf bytes.Buffer
	log func(string, ...any)
}

func (w *logWriter) Write(p []byte) (int, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.buf.Write(p)
	for {
		line, err := w.buf.ReadString('\n')
		if err != nil {
			// An unfinished line waits for the rest of it.
			w.buf.Reset()
			w.buf.WriteString(line)
			return len(p), nil
		}
		if text := strings.TrimSpace(line); text != "" {
			w.log("%s", text)
		}
	}
}
