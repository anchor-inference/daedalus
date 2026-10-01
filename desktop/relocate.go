package main

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

// Where the data lives. A desktop application keeps what it owns in the per-user folder its
// platform has for that, not beside its own executable: the executable is in a place an installer
// owns and the next version replaces (%LOCALAPPDATA%\Programs, /Applications, /opt), and a folder
// that holds both the program and the data is a folder the operator deletes to "remove the old
// version" and loses the conversations and the keys with it.
//
//	Windows  %LOCALAPPDATA%\Daedalus\data
//	macOS    ~/Library/Application Support/Daedalus/data
//	Linux    $XDG_DATA_HOME/daedalus/data (~/.local/share/daedalus/data)
//
// Beside it, under the same parent, are the runtime and the local state on Windows and macOS, and
// on every platform the update control folder `.daedalus-update/` (fence.go), so one folder is the
// whole of an installation's footprint there.
//
// %LOCALAPPDATA% and not Documents on Windows: Documents is very often redirected into OneDrive,
// and a folder full of git checkouts and a live SQLite database under a sync client is a folder
// whose files are locked, rewritten and conflict-copied behind the agent's back.
//
// Launchers before this one kept the data beside themselves — <folder>/data next to the executable,
// or next to Daedalus.app. That folder is still found and used, and the first ordinary start moves
// it here once (relocateData), so no command ever has to be told where an older installation is.

// standardDataDir is the per-user data folder described above.
func standardDataDir() (string, error) {
	switch runtime.GOOS {
	case "windows":
		base := os.Getenv("LOCALAPPDATA")
		if base == "" {
			return "", errors.New("%LOCALAPPDATA% is not set")
		}
		return filepath.Join(base, "Daedalus", "data"), nil
	case "darwin":
		home, err := os.UserHomeDir()
		if err != nil {
			return "", err
		}
		return filepath.Join(home, "Library", "Application Support", "Daedalus", "data"), nil
	}
	home, _ := os.UserHomeDir()
	share := xdgDir("XDG_DATA_HOME", home, ".local", "share")
	if share == "" {
		return "", errors.New("neither $XDG_DATA_HOME nor $HOME is set")
	}
	return filepath.Join(share, "daedalus", "data"), nil
}

// legacyDataDir is where a launcher from before the move kept the data of the installation this
// executable belongs to: beside the .app, or beside the executable. Empty when there is no
// executable to speak of.
func legacyDataDir(exe string) string {
	if exe == "" {
		return ""
	}
	if resolved, err := filepath.EvalSymlinks(exe); err == nil {
		exe = resolved
	}
	if app, ok := bundleRoot(exe); ok {
		return filepath.Join(filepath.Dir(app), "data")
	}
	return filepath.Join(filepath.Dir(exe), "data")
}

// looksLikeData says that a folder holds an installation and not only the empty skeleton a first
// start makes before its questions are answered: the configuration, the mode, a checkout or the
// native state. EnsureDirs makes daedalus-secrets and daedalus-host-terminals on any start, so
// neither of those counts.
func looksLikeData(dir string) bool {
	for _, name := range []string{".env", "mode", "daedalus", "state"} {
		if exists(filepath.Join(dir, name)) {
			return true
		}
	}
	return false
}

// DefaultDataDir is the data folder when --data does not name one: the standard folder once it
// holds an installation; otherwise an installation an older launcher left beside this executable,
// until the first ordinary start moves it; otherwise the standard folder, where a first start makes
// a new one.
func DefaultDataDir(exe string) string {
	standard, err := standardDataDir()
	if err != nil {
		// No home folder at all — a service account, a stripped environment. The working directory
		// is the only place left that the operator can see.
		return "data"
	}
	if looksLikeData(standard) {
		return standard
	}
	if legacy := legacyDataDir(exe); legacy != "" && looksLikeData(legacy) {
		return legacy
	}
	return standard
}

// relocatableFromHere reports the standard folder when the data folder in use is the one beside
// this executable that an older launcher made, and the standard folder has no installation of its
// own: the one case the first ordinary start moves on its own.
func relocatableFromHere(data string) (string, bool) {
	exe, err := os.Executable()
	if err != nil {
		return "", false
	}
	standard, err := standardDataDir()
	if err != nil || looksLikeData(standard) {
		return "", false
	}
	legacy := legacyDataDir(exe)
	if legacy == "" || !sameFolder(legacy, data) {
		return "", false
	}
	return standard, true
}

func sameFolder(a, b string) bool {
	left, err1 := filepath.Abs(a)
	right, err2 := filepath.Abs(b)
	if err1 != nil || err2 != nil {
		return false
	}
	if runtime.GOOS == "windows" || runtime.GOOS == "darwin" {
		return strings.EqualFold(filepath.Clean(left), filepath.Clean(right))
	}
	return filepath.Clean(left) == filepath.Clean(right)
}

// relocateData moves an installation's data folder from where an older launcher kept it to the
// standard folder, and returns the paths it now has. It is a rename and nothing else: a data folder
// is never copied here, because a copy is a second installation the moment either half is started.
// When a rename cannot do it — another disk, a folder something still has open — nothing moves, the
// error says why, and the caller goes on using the folder where it is.
//
// It refuses, changing nothing, while anything could be using the folder: an upgrade or update
// that did not finish, a launcher running on it (it holds the installation lock for as long as it
// runs), the stack's own processes left behind by a launcher that died, anything answering on the
// app's port, any process with a file open inside it.
//
// The update control folder beside the old data folder (fence.go) is not moved: the records in it
// name the data folder by its old path, and a kept copy is the operator's to delete. The note left
// in the old folder says where both are.
//
// The local state — logs, terminal logs, the browser profiles with their logins — is keyed by the
// data folder's path (local.go), so it is renamed to the new key with the data. The runtime is a
// cache whose interpreter and environments hold absolute paths; it is removed and rebuilt by the
// next start, as when the runtime first left the data folder.
func relocateData(ctx context.Context, from, to string, log func(string, ...any)) (Paths, error) {
	source, err := NewPaths(from)
	if err != nil {
		return Paths{}, err
	}
	if sameFolder(from, to) {
		return source, nil
	}
	if !looksLikeData(from) {
		return Paths{}, fmt.Errorf("%s holds no Daedalus installation", from)
	}
	if looksLikeData(to) {
		return Paths{}, fmt.Errorf("%s already holds an installation; it is not replaced", to)
	}
	if isInside(to, from) || isInside(from, to) {
		return Paths{}, fmt.Errorf("%s and %s are one inside the other", from, to)
	}
	if target, err := NewPaths(to); err == nil {
		if _, running := readInstance(target); running {
			return Paths{}, fmt.Errorf("Daedalus is running on %s; close it first", to)
		}
	}
	if err := InterruptedUpgrade(source); err != nil {
		return Paths{}, err
	}
	if _, running := readInstance(source); running {
		return Paths{}, fmt.Errorf("a launcher is running on %s; close it first", from)
	}
	lock, err := AcquireLock(source, "relocate")
	if err != nil {
		return Paths{}, err
	}
	locked := true
	release := func() {
		if locked {
			lock.Release()
			locked = false
		}
	}
	defer release()
	if err := StopOrphans(ctx, source, log); err != nil {
		return Paths{}, err
	}
	if port := APIPort(source); portAnswers(port) {
		return Paths{}, fmt.Errorf("something answers on the app's port %s: a stack may still be running out of %s", port, from)
	}
	if err := quiesceData(ctx, source); err != nil {
		return Paths{}, err
	}
	if err := os.MkdirAll(filepath.Dir(to), 0o755); err != nil {
		return Paths{}, err
	}
	// What a first start made at the standard folder before the move was asked for — the empty
	// secrets folder, a language — is set aside rather than deleted or merged: nothing in it is
	// needed, and nothing of the operator's is ever deleted on a guess.
	var aside string
	if _, err := os.Lstat(to); err == nil {
		aside = fmt.Sprintf("%s.before-move-%s", to, time.Now().UTC().Format("20060102T150405Z"))
		if err := os.Rename(to, aside); err != nil {
			return Paths{}, fmt.Errorf("%s exists and could not be set aside: %w", to, err)
		}
	}
	// On Windows the lock is an open file inside the folder, and a folder with an open file in it
	// cannot be renamed. Everything that could have taken it has been refused above; the moment
	// between this release and the rename is the one this platform leaves.
	if runtime.GOOS == "windows" {
		release()
	}
	if err := renameFolder(from, to); err != nil {
		if aside != "" {
			_ = os.Rename(aside, to)
		}
		return Paths{}, fmt.Errorf("%s could not be moved to %s (%w); it stays where it is and is used from there", from, to, err)
	}
	moved, err := NewPaths(to)
	if err != nil {
		return Paths{}, err
	}
	if exists(source.Local) && !exists(moved.Local) {
		if err := os.MkdirAll(filepath.Dir(moved.Local), 0o755); err == nil {
			if err := os.Rename(source.Local, moved.Local); err != nil {
				log("the local state stays at %s (%v): the logs, terminal logs and browser logins there are not carried over", source.Local, err)
			}
		}
	}
	if exists(source.Runtime) && !sameFolder(source.Runtime, moved.Runtime) {
		if err := os.RemoveAll(source.Runtime); err != nil {
			log("the old runtime at %s could not be removed (%v); it is a cache and may be deleted by hand", source.Runtime, err)
		}
	}
	writeMovedNote(from, to)
	log("the data folder moved from %s to %s", from, to)
	return moved, nil
}

// renameFolder renames, and on Windows tries again for a few seconds: an indexer or an antivirus
// scanner that opened a file a moment ago keeps the folder from being renamed until it lets go.
func renameFolder(from, to string) error {
	var err error
	for attempt := 0; attempt < 20; attempt++ {
		if err = os.Rename(from, to); err == nil || runtime.GOOS != "windows" {
			return err
		}
		time.Sleep(250 * time.Millisecond)
	}
	return err
}

// writeMovedNote leaves a line where the data folder was, for whoever opens that folder next.
func writeMovedNote(from, to string) {
	note := "The Daedalus data that was in this folder now lives in\n  " + to + "\n" +
		"(moved " + time.Now().UTC().Format(time.RFC3339) + ").\n"
	if control := fenceControlPath(from); exists(control) {
		note += "\nCopies of the data kept by earlier updates stay in\n  " + control + "\n" +
			"and can be deleted once the installation runs as it should.\n"
	}
	_ = os.WriteFile(filepath.Join(filepath.Dir(from), "DATA-MOVED.txt"), []byte(note), 0o644)
}

// importCommand is `daedalus-desktop import DIR`: an installation an older launcher kept somewhere
// else — a folder unpacked from a zip, with data/ beside the executable — brought into the folder
// this launcher uses, by the same move a start makes for the installation beside itself. DIR may
// be that data folder or the folder that holds it.
func importCommand(ctx context.Context, app *App, opts options) error {
	from, err := filepath.Abs(opts.extra)
	if err != nil {
		return err
	}
	if !looksLikeData(from) && looksLikeData(filepath.Join(from, "data")) {
		from = filepath.Join(from, "data")
	}
	say := func(format string, args ...any) { fmt.Printf(format+"\n", args...) }
	moved, err := relocateData(ctx, from, app.paths.Data, say)
	if err != nil {
		return err
	}
	say("Start Daedalus as usual: it uses %s now.", moved.Data)
	return nil
}
