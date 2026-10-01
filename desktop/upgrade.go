package main

import (
	"archive/tar"
	"archive/zip"
	"bufio"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

// `daedalus-desktop upgrade` moves an installation to a newer launcher release, and it is the only
// thing that does. The order is the whole design:
//
//  1. find a newer desktop-v* release with this machine's archive and SHA256SUMS attached;
//  2. say what will happen and wait for an explicit yes (or --yes);
//  3. download the archive, check it against SHA256SUMS and unpack it into a staging folder, with
//     every path checked — a failure here has changed nothing;
//  4. stop the stack, back the data folder up and read the backup back against its manifest;
//  5. swap the release's files into the installation folder, keeping the old ones beside them;
//  6. run the new launcher's `upgrade --finish`, which moves the checkouts the way the new launcher
//     does, starts the stack and waits for the app to answer. The app's migrations run here, which is
//     why the backup came first;
//  7. on success the upgrade is committed; on any failure after step 4 both the data (from the
//     verified backup) and the launcher's files are put back. A rollback that itself fails stops with
//     the paths needed to finish it by hand, and the launcher refuses to start until it is resolved.
//
// Every step is recorded in a journal before it starts, so an upgrade cut off by a crash or a power
// cut is found on the next start rather than half-applied in silence. The journal lives beside the
// data folder, in its control folder (fence.go), and never inside it: the writer fence exchanges the
// whole data folder, and a journal inside it would be exchanged too — a restore would make the
// journal from before the update live again, which says nothing is unfinished.
//
// SHA256SUMS comes from the same release as the archive and only proves the download is the file
// it lists; what proves who published the release is its signature by the project's release key,
// checked before anything is unpacked (signing.go, SIGNING.md).

// Journal kinds.
const (
	kindUpgrade = "upgrade"
	kindUpdate  = "update"
)

// Journal stages, in order.
const (
	stagePrepared       = "prepared"  // archive downloaded, checked and staged; nothing changed
	stageBackedUp       = "backed-up" // stack stopped, backup written and verified
	stageSwapped        = "swapped"   // the release's files are in place, the old ones kept
	stageFinishing      = "finishing" // the new launcher is updating and starting the stack
	stageCommitted      = "committed"
	stageRolledBack     = "rolled-back"
	stageRollbackFailed = "rollback-failed"
)

// Journal is the upgrade's own record.
type Journal struct {
	// Kind is upgrade (a new launcher release) or update (the same launcher moving the checkouts).
	Kind    string    `json:"kind,omitempty"`
	From    string    `json:"from"`
	To      string    `json:"to"`
	Mode    string    `json:"mode"`
	Root    string    `json:"root"`    // the installation folder the files are swapped in
	Work    string    `json:"work"`    // <root>/.daedalus-upgrade/<id>: new/ and old/
	Items   []string  `json:"items"`   // the top-level names the release replaces
	Swapped []string  `json:"swapped"` // the ones already swapped
	Backup  string    `json:"backup"`
	Stage   string    `json:"stage"`
	Error   string    `json:"error,omitempty"`
	Started time.Time `json:"started"`
	// LockToken is the installation lock's token while the upgrade that wrote this holds it.
	LockToken string `json:"lock_token,omitempty"`
	// Swapping is the item swap() is about to move, written before it moves it.
	Swapping string `json:"swapping,omitempty"`
	// FinishToken is the finish lock's token while `upgrade --finish` holds it.
	FinishToken string `json:"finish_token,omitempty"`
	// Fence and Pre are the fenced switch that protected the data and the kept copy it left, when
	// the fence was the protection (protect.go); Backup is empty then. PreKey is the kept copy's
	// identity (device and inode): a rollback cut off after it made that copy live again finds it at
	// the data folder by this, and does not try to restore it a second time.
	Fence  string    `json:"fence,omitempty"`
	Pre    string    `json:"pre,omitempty"`
	PreKey [2]uint64 `json:"pre_key,omitempty"`
	// RuntimeMovedOut says this upgrade moved a runtime from before the move out of the data folder;
	// a rollback to the launcher that used it puts it back.
	RuntimeMovedOut bool `json:"runtime_moved_out,omitempty"`
}

func journalFile(p Paths) string { return filepath.Join(fenceControlPath(p.Data), "upgrade.json") }

func readJournal(p Paths) (*Journal, error) {
	body, err := os.ReadFile(journalFile(p))
	if err != nil {
		return nil, err
	}
	var journal Journal
	if err := json.Unmarshal(body, &journal); err != nil {
		return nil, fmt.Errorf("%s: %w", journalFile(p), err)
	}
	return &journal, nil
}

// writeJournalHook lets a test stop an upgrade at an exact point, as a crash would. Nil otherwise.
var writeJournalHook func(*Journal)

func writeJournal(p Paths, journal *Journal) error {
	if err := os.MkdirAll(filepath.Dir(journalFile(p)), 0o700); err != nil {
		return err
	}
	body, err := json.MarshalIndent(journal, "", "  ")
	if err != nil {
		return err
	}
	if err := writeFileSync(journalFile(p), body, 0o600); err != nil {
		return err
	}
	if writeJournalHook != nil {
		writeJournalHook(journal)
	}
	return nil
}

// unresolved reports whether a journal is an upgrade that neither finished nor was undone.
func (j *Journal) unresolved() bool {
	switch j.Stage {
	case stageCommitted, stageRolledBack, stagePrepared:
		return false
	}
	return true
}

// InterruptedUpgrade is the refusal every command but upgrade meets while an upgrade is unresolved:
// starting a stack on data an upgrade may have half-migrated, under a launcher that may be either
// version, is exactly what the journal is there to prevent.
func InterruptedUpgrade(p Paths) error { return interruptedUpgrade(p, nil) }

// interruptedUpgrade is InterruptedUpgrade for a process that may itself hold the lock (own).
func interruptedUpgrade(p Paths, own *InstallLock) error {
	// An upgrade, update or rollback running right now holds the installation lock from its first
	// step to its last — the backup included — and nothing else starts until it lets go.
	if holder, held := LockHeldByOther(p, own); held && holder.Kind != "launcher" {
		return fmt.Errorf("%w: %s", errLocked, describeHolder(holder, true))
	}
	journal, err := readJournal(p)
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("the update journal cannot be read, so nothing starts: %w", err)
	}
	if err == nil && journal.unresolved() {
		if journal.Kind == kindUpdate {
			return fmt.Errorf("an update of the checkouts did not finish (stage %q%s).\n"+
				"Nothing starts until it is resolved. Run `daedalus-desktop upgrade --rollback --data %s` to put the "+
				"data back as it was before it; %s.",
				journal.Stage, errSuffix(journal.Error), p.Data, keptDescription(p, journal))
		}
		return fmt.Errorf("an upgrade from %s to %s did not finish (stage %q%s).\n"+
			"Nothing starts until it is resolved. Run `daedalus-desktop upgrade --rollback --data %s` to put the "+
			"launcher and the data back as they were before it; %s. If the launcher is missing from %s, "+
			"run that command with the previous launcher kept in %s.",
			journal.From, journal.To, journal.Stage, errSuffix(journal.Error), p.Data, keptDescription(p, journal), journal.Root, filepath.Join(journal.Work, "old"))
	}
	// A switch of the data folder that did not reach its end: which tree is live is for `update
	// resolve` to establish, and a stack started before that would run on whichever it is.
	for _, item := range fenceSummarize(fenceControlPath(p.Data)).Items {
		if item.Kind == "unfinished" {
			return fmt.Errorf("a switch of the data folder did not finish (%s, stopped at %s).\n"+
				"Nothing starts until it is settled. `daedalus-desktop update resolve --data %s` says what is where; "+
				"with --apply it settles it without touching the live data", item.Path, item.Detail, p.Data)
		}
	}
	return nil
}

// keptDescription says where the data from before an update is: the kept copy the fence left, or
// the verified backup.
func keptDescription(p Paths, journal *Journal) string {
	switch {
	case journal.Pre != "":
		return "the data from before it is kept whole at " + filepath.Join(fenceControlPath(p.Data), "retained", journal.Pre)
	case journal.Backup != "":
		return "the verified backup is " + journal.Backup
	}
	return "it had not yet changed the data"
}

func errSuffix(message string) string {
	if message == "" {
		return ""
	}
	return ": " + message
}

// upgradeStack is what the upgrade needs from the stack. appStack is the real one; the tests and
// the smoke script (built with -tags upgradefixture) use a fixture.
type upgradeStack interface {
	Configured() bool
	Stop(ctx context.Context) error
	// Snapshot and Restore cover what lives outside the data folder: Docker's volumes and images.
	Snapshot(ctx context.Context, backup string, manifest *Manifest) error
	Restore(ctx context.Context, backup string, manifest *Manifest) error
	// Prepare fetches what the update will move the checkouts to and builds its environment beside
	// the one in use, before anything is stopped or switched. Nothing of the installation changes.
	Prepare(ctx context.Context) error
	// UpdateAndCheck moves the checkouts, starts the stack and waits for the app to answer.
	UpdateAndCheck(ctx context.Context) error
	// Leave is called after a successful upgrade: native mode stops the processes the launcher is
	// about to stop holding up anyway.
	Leave(ctx context.Context)
}

// handoverHook and finishStartHook let the smoke's fixture build pause at the two ends of the
// hand-over — the old launcher just before it starts the new one, the new one before it takes the
// lock over — so a test can kill the old launcher at exactly those moments. Nil in a real build.
var handoverHook, finishStartHook func()

// fixtureStack is set by fixture_stack.go in a build tagged upgradefixture, and nil otherwise.
var fixtureStack func(p Paths, log func(string, ...any)) upgradeStack

// upgradeOptions are the flags of the upgrade command.
type upgradeOptions struct {
	yes          bool
	finish       bool
	rollback     bool
	executable   string // the running launcher; os.Executable() when empty
	stdin        io.Reader
	stdinIsTTY   bool
	now          func() time.Time
	runNewBinary func(ctx context.Context, exe string, args []string) error

	// The bridge: the new launcher, run by an installer from its download, upgrading the
	// installation at bridgeRoot whose launcher predates this command.
	bridge        bool
	bridgeRoot    string
	bridgeArchive string
	bridgeSums    string
	self          string // the running launcher's file, for the bridge's check; os.Executable() when empty
	probeVersion  func(ctx context.Context, exe string) (string, error)

	// damageBackup lets a test break a backup between writing and verifying it.
	damageBackup func(dir string)
	// finishLock hands the finish lock to an in-process --finish, as a descriptor does between
	// processes; a test that runs both halves in one process sets it.
	finishLock *InstallLock
	// checkRoom replaces the free-space check in a test.
	checkRoom func(p Paths, root string, items []string) error
}

// Upgrader carries one upgrade.
type Upgrader struct {
	paths Paths
	stack upgradeStack
	mode  Mode
	opts  upgradeOptions
	out   io.Writer

	// lock is the installation lock this upgrade holds, or the launcher's when the update runs
	// inside a launcher (inLauncher). Nil until Run takes it.
	lock       *InstallLock
	inLauncher bool
	// finishLock is the finish lock an upgrade's first launcher holds and hands to --finish.
	finishLock *InstallLock
}

// takeLock takes the installation lock for the whole of one operation, or says who has it.
func (u *Upgrader) takeLock(kind string) (func(), error) {
	if u.lock != nil {
		return func() {}, nil
	}
	lock, err := AcquireLock(u.paths, kind)
	if err != nil {
		return nil, fmt.Errorf("nothing was changed: %w", err)
	}
	u.lock = lock
	return func() { lock.Release(); u.lock = nil }, nil
}

func (u *Upgrader) say(format string, args ...any) { fmt.Fprintf(u.out, format+"\n", args...) }

// UpgradeCommand is the entry point from main.
func UpgradeCommand(ctx context.Context, app *App, opts options) error {
	ignoreHangup()
	stack := chooseStack(app)
	if opts.root != "" {
		root, err := filepath.Abs(opts.root)
		if err != nil {
			return err
		}
		opts.root = root
	}
	info, _ := os.Stdin.Stat()
	u := &Upgrader{paths: app.paths, stack: stack, mode: app.Mode(), out: os.Stdout, opts: upgradeOptions{
		yes: opts.yes, finish: opts.finish, rollback: opts.rollback,
		bridge: opts.bridge, bridgeRoot: opts.root, bridgeArchive: opts.archive, bridgeSums: opts.sums,
		stdin: os.Stdin, stdinIsTTY: info != nil && info.Mode()&os.ModeCharDevice != 0,
	}}
	return u.Run(ctx)
}

// chooseStack is the real stack, or the smoke test's fixture in a build that has one and a data
// folder that carries its marker.
func chooseStack(app *App) upgradeStack {
	if fixtureStack != nil && os.Getenv("DAEDALUS_UPGRADE_FIXTURE") != "" {
		return fixtureStack(app.paths, app.log)
	}
	return appStack{app}
}

func (u *Upgrader) now() time.Time {
	if u.opts.now != nil {
		return u.opts.now()
	}
	return time.Now()
}

// Run dispatches on the three shapes of the command.
func (u *Upgrader) Run(ctx context.Context) error {
	switch {
	case u.opts.rollback:
		release, err := u.takeLock("rollback")
		if err != nil {
			return err
		}
		defer release()
		journal, err := readJournal(u.paths)
		if err != nil {
			return fmt.Errorf("there is no upgrade to roll back: %w", err)
		}
		if !journal.unresolved() {
			return fmt.Errorf("the last upgrade (%s → %s) is %s; there is nothing to roll back", journal.From, journal.To, journal.Stage)
		}
		// A rollback that was asked for and finished is a success: its exit status says so, and only a
		// rollback that could not finish is a failure.
		if err := u.rollback(ctx, journal, errRollbackRequested); !errors.Is(err, errRollbackRequested) {
			return err
		}
		return nil
	case u.opts.finish:
		return u.finish(ctx)
	default:
		if u.mode == ModeDocker {
			// Refused before anything at all is written, the lock file included.
			return errDockerNotCovered
		}
		release, err := u.takeLock("upgrade")
		if err != nil {
			return err
		}
		defer release()
		// The finish lock too, from the first step: the new launcher inherits it (see finish), and
		// holding it from here means there is no hand-over moment to fall into.
		finishLock, err := AcquireFinishLock(u.paths)
		if err != nil {
			return fmt.Errorf("nothing was changed: %w", err)
		}
		defer finishLock.Release()
		u.finishLock = finishLock
		defer func() { u.finishLock = nil }()
		return u.begin(ctx)
	}
}

// upgradeSource is where the release comes from: the network (a launcher upgrading itself) or two
// files an installer already downloaded (the bridge, run by the new launcher for an installation
// whose launcher is too old to have this command).
type upgradeSource struct {
	from, to, notes, asset string
	fetch                  func(ctx context.Context) (archive, sums []byte, err error)
	// signature fetches SHA256SUMS.sig, or answers nil when the release has none (signing.go).
	signature func(ctx context.Context) ([]byte, error)
	sig       []byte
}

// preflight is what is refused before anything is read from the network or written anywhere.
func (u *Upgrader) preflight() error {
	if holder, held := LockHeldByOther(u.paths, u.lock); held {
		return fmt.Errorf("%w: %s", errLocked, describeHolder(holder, true))
	}
	if err := interruptedUpgrade(u.paths, u.lock); err != nil {
		return err
	}
	if _, running := readInstance(u.paths); running {
		return errors.New("a launcher is running on this installation; close it first — an upgrade stops the stack and replaces the launcher's files")
	}
	if u.mode == ModeDocker {
		// Fail closed. The volumes and the images are code here (docker_backup.go) and unit-tested,
		// but a restore and a health check on real Docker volumes have not been run, and the next
		// start pulls :latest again whatever a rollback put back. Until both are proven, Docker mode
		// is not upgraded by this command at all.
		return errDockerNotCovered
	}
	return nil
}

// errRollbackRequested is the cause of a rollback the operator asked for.
var errRollbackRequested = errors.New("rolled back on request")

// errDockerNotCovered is the refusal for Docker mode, from upgrade and from update alike.
var errDockerNotCovered = errors.New("updating a Docker installation from the launcher is not available yet: restoring Docker's " +
	"volumes and images after a failed update has not been proven, and the next start would pull :latest again. " +
	"Nothing was changed. desktop/UPDATES.md (\"Docker mode\") gives the manual path: back the volumes up with docker " +
	"commands first, then pull and restart with compose")

// begin is steps 1 to 6, in the launcher being replaced — or, for the bridge, in the new launcher
// run from the installer's download.
func (u *Upgrader) begin(ctx context.Context) error {
	if err := u.preflight(); err != nil {
		return err
	}
	var (
		source     upgradeSource
		root, item string
		err        error
	)
	if u.opts.bridge {
		root = u.opts.bridgeRoot
		item = launcherItem(runtime.GOOS)
		if source, err = u.bridgeSource(ctx, root, item); err != nil {
			return err
		}
	} else {
		u.say("Looking for a newer launcher than %s...", version)
		offer, release, err := FindUpgrade(ctx, version)
		if err != nil {
			return err
		}
		if offer == nil {
			u.say("This is the newest launcher release; nothing to do.")
			return nil
		}
		u.say("Note: %s.", authenticationNote())
		source = networkSource(offer, release)
		exe := u.opts.executable
		if exe == "" {
			if exe, err = os.Executable(); err != nil {
				return err
			}
			if resolved, err := filepath.EvalSymlinks(exe); err == nil {
				exe = resolved
			}
		}
		root, item = installRoot(exe)
		// A copy the operator cannot replace — an AppImage, or files a package manager put in a
		// folder only the administrator may write — is refused before anything is asked or written,
		// with what to do instead.
		if why := notSelfReplaceable(root, os.Getenv); why != "" {
			return errors.New(why)
		}
	}
	if isInside(u.paths.Data, filepath.Join(root, item)) {
		return fmt.Errorf("the data folder %s is inside what an upgrade replaces", u.paths.Data)
	}
	u.say("")
	u.say("%s is available (this installation's launcher is %s).", source.to, source.from)
	if source.notes != "" {
		u.say("Release notes: %s", source.notes)
	}
	u.say("")
	u.say("What happens if you go ahead:")
	u.say("  1. SHA256SUMS is checked against the release key's signature, and %s against", source.asset)
	u.say("     SHA256SUMS: a release the project's key did not sign is refused")
	if u.stack.Configured() {
		u.say("  2. the stack is stopped")
	}
	if how, _, _ := chooseProtection(u.paths, u.mode); how == protectFence {
		u.say("  3. the data folder %s is switched to a copy while the kernel keeps every writer out;", u.paths.Data)
		u.say("     the folder as it was is kept whole in %s", filepath.Join(fenceControlPath(u.paths.Data), "retained"))
	} else {
		u.say("  3. the data folder %s and the launcher's files are backed up into %s,", u.paths.Data, backupsDir(u.paths))
		u.say("     and the backup is read back and checked before anything is replaced")
		u.say("     (the backup holds your keys too; it is readable only by you)")
	}
	u.say("  4. the launcher's files in %s are replaced; the old ones are kept", root)
	if u.stack.Configured() {
		u.say("  5. the new launcher updates the checkouts and starts the stack — the app's database")
		u.say("     migrations run here — and waits for the app to answer")
	}
	u.say("  If anything fails after the backup, the data and the launcher are put back as they were.")
	u.say("")
	if err := u.confirm("upgrade"); err != nil {
		return err
	}

	journal := &Journal{Kind: kindUpgrade, From: source.from, To: source.to, Mode: string(u.mode), Root: root, Stage: stagePrepared, Started: u.now().UTC(), LockToken: u.lock.Token(), FinishToken: u.finishLock.Token()}
	journal.Work = filepath.Join(root, ".daedalus-upgrade", u.now().UTC().Format("20060102T150405Z"))
	cleanOldWork(filepath.Dir(journal.Work))
	if err := u.prepare(ctx, journal, source, root, item); err != nil {
		// Nothing was replaced. What the attempt made — the staging folder, the journal, a backup
		// that did not verify — goes too, and a runtime it moved out of the data folder comes back,
		// so a refusal leaves the installation as it was. When the runtime cannot come back, the
		// refusal says so rather than that nothing changed.
		if note := u.abandon(journal); note != "" {
			return fmt.Errorf("%w; but %s", err, note)
		}
		return err
	}

	// From here on the installation changes, and every failure is rolled back.
	u.say("Replacing the launcher's files...")
	if err := u.swap(journal); err != nil {
		return u.rollback(ctx, journal, err)
	}
	journal.Stage = stageSwapped
	if err := writeJournal(u.paths, journal); err != nil {
		return u.rollback(ctx, journal, err)
	}

	next := filepath.Join(root, executableIn(item))
	if handoverHook != nil {
		handoverHook()
	}
	u.say("Handing over to %s...", source.to)
	run := u.opts.runNewBinary
	if run == nil {
		run = func(ctx context.Context, exe string, args []string) error {
			return runBinary(ctx, exe, args, u.finishLock)
		}
	}
	runErr := run(ctx, next, []string{"upgrade", "--finish", "--data", u.paths.Data})
	after, err := readJournal(u.paths)
	if err != nil {
		return u.rollback(ctx, journal, fmt.Errorf("the journal is unreadable after the new launcher ran: %w", err))
	}
	switch after.Stage {
	case stageCommitted:
		return nil
	case stageRolledBack:
		// The new launcher has already said why; this is the exit status.
		return fmt.Errorf("not upgraded: %s is back in place", after.From)
	case stageRollbackFailed:
		return errors.New(rollbackFailedMessage(u.paths, after))
	}
	// The new launcher never got as far as deciding: it did not start, or does not know --finish.
	if runErr == nil {
		runErr = fmt.Errorf("the new launcher stopped at stage %q", after.Stage)
	}
	return u.rollback(ctx, after, fmt.Errorf("the new launcher did not finish the upgrade: %w", runErr))
}

// prepare is everything before the first file is replaced: the download staged and checked, the
// stack stopped, and the backup written and verified. A failure here changes nothing.
func (u *Upgrader) prepare(ctx context.Context, journal *Journal, source upgradeSource, root, item string) error {
	if err := os.MkdirAll(filepath.Join(journal.Work, "old"), 0o700); err != nil {
		return err
	}
	archive, sums, err := source.fetch(ctx)
	if err != nil {
		return err
	}
	if source.signature != nil {
		if source.sig, err = source.signature(ctx); err != nil {
			return err
		}
	}
	staged := filepath.Join(journal.Work, "new")
	items, err := u.stage(archive, sums, source, staged, filepath.Base(u.paths.Data))
	if err != nil {
		return err
	}
	if u.opts.bridge {
		// The bridge runs code the installer downloaded; it goes on only if that code is exactly the
		// launcher in the archive it is about to install, so what does the upgrade is what it installs.
		if err := sameFile(u.self(), filepath.Join(staged, executableIn(item))); err != nil {
			return fmt.Errorf("the launcher running this bridge is not the one in %s: %w", source.asset, err)
		}
	}
	journal.Items = items
	if err := writeJournal(u.paths, journal); err != nil {
		return err
	}

	if err := u.stack.Prepare(ctx); err != nil {
		return fmt.Errorf("the next version's environment could not be prepared, so nothing was changed: %w", err)
	}
	moved, err := u.quiesce(ctx)
	if err != nil {
		return err
	}
	journal.RuntimeMovedOut = moved
	var present []string
	for _, name := range items {
		if _, err := os.Lstat(filepath.Join(root, name)); err == nil {
			present = append(present, name)
		}
	}
	if err := u.protect(ctx, journal, root, present); err != nil {
		return err
	}
	journal.Stage = stageBackedUp
	if err := writeJournal(u.paths, journal); err != nil {
		return err
	}
	return nil
}

// quiesce stops everything that may write into the data folder, and makes sure nothing does, before
// it is protected: the stack, then the services the agent started and left running, then a look at
// every process for a working directory or an open file under the data folder. Only then does a
// runtime from before the move leave the data folder — a v0.12 installation above all; the fence
// refuses a data folder that still holds one — because moving it from under a process that still
// runs out of it would pull its interpreter away mid-run.
// It says whether a runtime was moved out.
func (u *Upgrader) quiesce(ctx context.Context) (bool, error) {
	if u.stack.Configured() {
		u.say("Stopping the stack...")
		if err := u.stack.Stop(ctx); err != nil {
			return false, fmt.Errorf("the stack could not be stopped, so nothing was changed: %w", err)
		}
	}
	if err := quiesceData(ctx, u.paths); err != nil {
		return false, fmt.Errorf("the data is still in use, so nothing was changed: %w", err)
	}
	legacy := exists(u.paths.LegacyRuntime)
	if err := migrateLegacyRuntime(ctx, u.paths, func(format string, args ...any) { u.say(format, args...) }); err != nil {
		return false, fmt.Errorf("moving the runtime out of the data folder: %w; nothing else was changed", err)
	}
	return legacy && !exists(u.paths.LegacyRuntime), nil
}

// abandon removes what a prepare that failed left behind.
func (u *Upgrader) abandon(journal *Journal) string {
	note := ""
	if journal.RuntimeMovedOut && !putLegacyRuntimeBack(u.paths, u.say) {
		note = fmt.Sprintf("the runtime folder had been moved out of %s first and could not be put back: the previous launcher downloads it again on its next start (its browser logins and terminal state are kept in %s)", u.paths.Data, u.paths.Local)
	}
	if journal.Work != "" {
		_ = os.RemoveAll(journal.Work)
		_ = os.Remove(filepath.Dir(journal.Work)) // only when empty
	}
	if journal.Backup != "" {
		_ = os.RemoveAll(journal.Backup)
		_ = os.Remove(backupsDir(u.paths))
	}
	if current, err := readJournal(u.paths); err == nil && current.Started.Equal(journal.Started) && current.Kind == journal.Kind {
		_ = os.Remove(journalFile(u.paths))
		// The folders the journal made, only when nothing else is in them.
		_ = os.Remove(filepath.Dir(journalFile(u.paths)))
		_ = os.Remove(filepath.Dir(filepath.Dir(journalFile(u.paths))))
	}
	return note
}

// confirm waits for an explicit yes, or takes --yes.
func (u *Upgrader) confirm(what string) error {
	if u.opts.yes {
		return nil
	}
	if !u.opts.stdinIsTTY {
		return fmt.Errorf("an %s needs a yes: run it in a terminal, or pass --yes", what)
	}
	fmt.Fprintf(u.out, "Type yes to %s: ", what)
	answer, err := bufio.NewReader(u.opts.stdin).ReadString('\n')
	if err != nil && strings.TrimSpace(answer) == "" {
		// /dev/null is a character device too, so this is where a run with no one to answer ends
		// up: nothing was read at all.
		fmt.Fprintln(u.out)
		return fmt.Errorf("an %s needs a yes: nothing was answered; run it in a terminal, or pass --yes", what)
	}
	if strings.TrimSpace(strings.ToLower(answer)) != "yes" {
		return fmt.Errorf("not done: the answer was not yes")
	}
	return nil
}

// backUp takes the backup (the data, plus the launcher's files when items are given), adds what
// lives outside the folder, and reads it all back. A failure anywhere here has changed nothing.
func (u *Upgrader) backUp(ctx context.Context, root string, items []string) (string, error) {
	u.say("Backing up %s...", u.paths.Data)
	room := checkRoom
	if u.opts.checkRoom != nil {
		room = u.opts.checkRoom
	}
	if err := room(u.paths, root, items); err != nil {
		return "", fmt.Errorf("%w; nothing was changed", err)
	}
	backup, manifest, err := CreateBackup(u.paths, version, string(u.mode), u.now(), root, items)
	if err != nil {
		return "", fmt.Errorf("%w; nothing was changed", err)
	}
	// A backup that is not complete and checked is not kept: nobody should find it later and trust it.
	discard := func() { _ = os.RemoveAll(backup); _ = os.Remove(backupsDir(u.paths)) }
	if err := u.stack.Snapshot(ctx, backup, manifest); err != nil {
		discard()
		return "", fmt.Errorf("the backup is incomplete, so nothing was changed: %w", err)
	}
	if err := writeManifest(backup, manifest); err != nil {
		discard()
		return "", fmt.Errorf("%w; nothing was changed", err)
	}
	if u.opts.damageBackup != nil {
		u.opts.damageBackup(backup)
	}
	if _, err := VerifyBackup(backup); err != nil {
		discard()
		return "", fmt.Errorf("the backup did not verify, so nothing was changed: %w", err)
	}
	u.say("Backup written and verified: %s (%d data entries, %d launcher entries).", backup, len(manifest.Entries), len(manifest.LauncherEntries))
	return backup, nil
}

func networkSource(offer *Offer, release *Release) upgradeSource {
	return upgradeSource{from: offer.From, to: offer.To, notes: offer.URL, asset: offer.Asset,
		fetch: func(ctx context.Context) ([]byte, []byte, error) {
			asset, _ := release.asset(offer.Asset)
			sums, _ := release.asset("SHA256SUMS")
			body, err := getLimited(ctx, asset.URL, assetLimit)
			if err != nil {
				return nil, nil, err
			}
			list, err := getLimited(ctx, sums.URL, 1<<20)
			if err != nil {
				return nil, nil, err
			}
			return body, list, nil
		},
		signature: func(ctx context.Context) ([]byte, error) {
			asset, ok := release.asset(signatureAsset)
			if !ok {
				return nil, nil
			}
			return getLimited(ctx, asset.URL, 4096)
		}}
}

// bridgeSource is the release an installer downloaded, for the installation at root whose launcher
// predates `upgrade`. The installed launcher is asked its version; it has to be an older release.
func (u *Upgrader) bridgeSource(ctx context.Context, root, item string) (upgradeSource, error) {
	if root == "" || u.opts.bridgeArchive == "" || u.opts.bridgeSums == "" {
		return upgradeSource{}, errors.New("--bridge needs --root, --archive and --sums")
	}
	if !parseVersion(version).ok {
		return upgradeSource{}, fmt.Errorf("this launcher is %q, not a release build; it does not bridge anything", version)
	}
	installed := filepath.Join(root, executableIn(item))
	if info, err := os.Stat(installed); err != nil || !info.Mode().IsRegular() {
		return upgradeSource{}, fmt.Errorf("there is no launcher at %s to upgrade", installed)
	}
	probe := u.opts.probeVersion
	if probe == nil {
		probe = probeVersion
	}
	from, err := probe(ctx, installed)
	if err != nil {
		return upgradeSource{}, fmt.Errorf("the installed launcher did not say its version: %w", err)
	}
	if !parseVersion(from).ok {
		return upgradeSource{}, fmt.Errorf("the installed launcher is %q, not a release; it is not upgraded by the installer", from)
	}
	if !parseVersion(version).newerThan(parseVersion(from)) {
		return upgradeSource{}, fmt.Errorf("the installed launcher is %s, not older than %s; nothing to do", from, version)
	}
	asset, err := platformAsset(runtime.GOOS, runtime.GOARCH)
	if err != nil {
		return upgradeSource{}, err
	}
	if filepath.Base(u.opts.bridgeArchive) != asset {
		return upgradeSource{}, fmt.Errorf("%s is not %s, the archive for this machine", u.opts.bridgeArchive, asset)
	}
	return upgradeSource{from: from, to: version, asset: asset, fetch: func(context.Context) ([]byte, []byte, error) {
		body, err := readLimited(u.opts.bridgeArchive, assetLimit)
		if err != nil {
			return nil, nil, err
		}
		list, err := readLimited(u.opts.bridgeSums, 1<<20)
		if err != nil {
			return nil, nil, err
		}
		return body, list, nil
	}, signature: func(context.Context) ([]byte, error) {
		// The installer puts SHA256SUMS.sig beside SHA256SUMS when the release has one.
		sig, err := readLimited(u.opts.bridgeSums+".sig", 4096)
		if os.IsNotExist(err) {
			return nil, nil
		}
		return sig, err
	}}, nil
}

func readLimited(name string, limit int64) ([]byte, error) {
	file, err := os.Open(name)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	body, err := io.ReadAll(io.LimitReader(file, limit+1))
	if err != nil {
		return nil, err
	}
	if int64(len(body)) > limit {
		return nil, fmt.Errorf("%s is larger than %d bytes", name, limit)
	}
	return body, nil
}

// probeVersion runs `<launcher> --version`. Every release so far prints its tag and nothing else.
func probeVersion(ctx context.Context, exe string) (string, error) {
	ctx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, exe, "--version").Output()
	if err != nil {
		return "", err
	}
	// The first word: a launcher built with pinned commits prints them after its tag
	// ("desktop-v1.0.0 daedalus@… protocore-exp@…"), and the tag is what is compared.
	fields := strings.Fields(string(out))
	if len(fields) == 0 {
		return "", errors.New("it printed nothing")
	}
	return fields[0], nil
}

// launcherItem is the top-level name that holds the launcher in an installation folder.
func launcherItem(goos string) string {
	switch goos {
	case "darwin":
		return "Daedalus.app"
	case "windows":
		return "daedalus-desktop.exe"
	}
	return "daedalus-desktop"
}

func (u *Upgrader) self() string {
	if u.opts.self != "" {
		return u.opts.self
	}
	exe, err := os.Executable()
	if err != nil {
		return ""
	}
	if resolved, err := filepath.EvalSymlinks(exe); err == nil {
		return resolved
	}
	return exe
}

func sameFile(a, b string) error {
	left, _, err := sha256File(a)
	if err != nil {
		return err
	}
	right, _, err := sha256File(b)
	if err != nil {
		return err
	}
	if left != right {
		return errors.New("their SHA-256 differ")
	}
	return nil
}

// finish is steps 6 and 7, in the new launcher.
func (u *Upgrader) finish(ctx context.Context) error {
	journal, err := readJournal(u.paths)
	if err != nil {
		return fmt.Errorf("--finish is run by an upgrade, and there is none in progress: %w", err)
	}
	if journal.Stage != stageSwapped {
		return fmt.Errorf("the upgrade is at stage %q, not one --finish continues", journal.Stage)
	}
	// The finish lock was taken by the launcher that began this upgrade, before it wrote the first
	// journal entry, and is handed to this process as an inherited descriptor: the same open lock,
	// held by both. So there is no moment between the two processes when nobody holds it, and if that
	// launcher dies now, this process still does — nothing can roll back under it.
	if finishStartHook != nil {
		finishStartHook()
	}
	finishLock, err := u.inheritedFinish(journal)
	if err != nil {
		return err
	}
	defer finishLock.Release()
	u.lock, u.finishLock = finishLock, finishLock
	defer func() { u.lock, u.finishLock = nil, nil }()
	journal.Stage = stageFinishing
	if err := writeJournal(u.paths, journal); err != nil {
		return u.rollback(ctx, journal, err)
	}
	if version != journal.To {
		return u.rollback(ctx, journal, fmt.Errorf("the launcher now in place says it is %s, not %s", version, journal.To))
	}
	if u.stack.Configured() {
		u.say("Updating the checkouts and starting the stack...")
		if err := u.stack.UpdateAndCheck(ctx); err != nil {
			return u.finishFailed(ctx, journal, fmt.Errorf("the stack did not come up on %s: %w", journal.To, err))
		}
	}
	u.stack.Leave(ctx)
	// Ownership before the commit: the journal must still be this finish's, at the stage it left
	// it. The finish lock makes anything else impossible; this makes sure the record agrees before
	// it says "committed".
	if current, err := readJournal(u.paths); err != nil || current.Stage != stageFinishing || current.FinishToken == "" || current.FinishToken != journal.FinishToken {
		return fmt.Errorf("the upgrade's journal is no longer this finish's (%v); not committing", journalDescription(current, err))
	}
	journal.Stage = stageCommitted
	if err := writeJournal(u.paths, journal); err != nil {
		return u.rollback(ctx, journal, err)
	}
	markWorkFinished(journal)
	u.cleanUpAfter(journal)
	noteInstalledVersion(journal.Root, journal.To)
	u.say("")
	u.say("Upgraded to %s.", journal.To)
	u.say("As for the data, %s; the previous launcher's files are in %s.", keptDescription(u.paths, journal), filepath.Join(journal.Work, "old"))
	u.say("Start the launcher as usual.")
	return nil
}

// rollback puts everything back: the stack stopped, the data from the backup, what lives in Docker,
// and the launcher's files. The data goes first — it is what cannot be fetched again. Any failure
// stops the rollback where it is, records the stage as rollback-failed and says what is left.
func (u *Upgrader) rollback(ctx context.Context, journal *Journal, cause error) error {
	// A rollback runs to its end: the Ctrl+C that may have started it does not stop it half-way.
	ctx = context.WithoutCancel(ctx)
	u.say("")
	u.say("Rolling back: %v", cause)
	fail := func(step string, err error) error {
		journal.Stage = stageRollbackFailed
		journal.Error = fmt.Sprintf("%v; rollback stopped at %s: %v", cause, step, err)
		_ = writeJournal(u.paths, journal)
		return errors.New(rollbackFailedMessage(u.paths, journal))
	}
	restoreData := journal.Stage == stageFinishing || journal.Stage == stageRollbackFailed
	if u.stack.Configured() && journal.Stage != stagePrepared {
		if err := u.stack.Stop(ctx); err != nil {
			if restoreData {
				// The data is never restored under a stack that may still be running: the rollback stops
				// here, with the data as it is, and says what to do.
				return fail("stopping the stack before restoring the data", fmt.Errorf("%w. Stop every Daedalus process of this installation (supervisor, the bot, ptyd, browserd, the key proxy) and run the rollback again", err))
			}
			u.say("the stack could not be stopped (%v); carrying on — the data was not changed by this upgrade", err)
		}
	}
	if restoreData {
		if err := quiesceData(ctx, u.paths); err != nil {
			return fail("stopping data writers before restoring the data", err)
		}
	}
	if restoreData {
		if err := u.restoreProtected(ctx, journal); err != nil {
			return fail("putting the data from before back", err)
		}
	}
	if err := unswap(journal); err != nil {
		_ = writeJournal(u.paths, journal)
		return fail("putting the previous launcher back", err)
	}
	if journal.RuntimeMovedOut {
		putLegacyRuntimeBack(u.paths, u.say)
	}
	journal.Stage = stageRolledBack
	journal.Error = cause.Error()
	if err := writeJournal(u.paths, journal); err != nil {
		return fail("recording the rollback", err)
	}
	markWorkFinished(journal)
	if journal.Kind == kindUpdate {
		u.say("Rolled back: the data and the checkouts are as they were before the update. Start the launcher as usual.")
		if errors.Is(cause, errRollbackRequested) {
			return cause
		}
		return fmt.Errorf("the update failed and was rolled back: %w", cause)
	}
	u.say("Rolled back to %s. Start the launcher as usual.", journal.From)
	if errors.Is(cause, errRollbackRequested) {
		return cause
	}
	return fmt.Errorf("upgrade to %s failed and was rolled back: %w", journal.To, cause)
}

// finishFailed is a --finish whose new version did not come up. Putting the data back from a
// fenced copy is a switch of its own, and a switch needs the installation lock — which the launcher
// that began the upgrade holds for as long as it lives, waiting for this process. So when it is
// alive, this process stops the stack, records why, and leaves the rollback to it: it rolls back
// any upgrade this process did not decide. When it is gone, the lock is free, and this process takes
// it and rolls back itself. A backup is restored here as it always was: it needs no lock.
func (u *Upgrader) finishFailed(ctx context.Context, journal *Journal, cause error) error {
	if journal.Pre == "" {
		return u.rollback(ctx, journal, cause)
	}
	lock, err := acquireAt(u.paths, lockPath(u.paths), "rollback")
	if err == nil {
		defer lock.Release()
		u.lock = lock
		return u.rollback(ctx, journal, cause)
	}
	if !errors.Is(err, errLocked) {
		return u.rollback(ctx, journal, fmt.Errorf("%v; and the installation lock could not be read: %w", cause, err))
	}
	u.say("The new version did not come up: %v", cause)
	if u.stack.Configured() {
		if err := u.stack.Stop(ctx); err != nil {
			u.say("the stack could not be stopped: %v", err)
		}
	}
	journal.Error = cause.Error()
	if err := writeJournal(u.paths, journal); err != nil {
		return err
	}
	u.say("The launcher that began the upgrade puts the data and itself back.")
	return fmt.Errorf("not upgraded: %w", cause)
}

// rollbackFailedMessage is what the operator is told when a rollback could not finish: what went
// wrong, that nothing must start, and where everything needed to finish by hand is.
func rollbackFailedMessage(p Paths, journal *Journal) string {
	if journal.Pre != "" {
		return fmt.Sprintf("the %s failed and its rollback could not finish: %s\n"+
			"Do not start the launcher. The data folder from before the %s is kept whole at %s;\n"+
			"`daedalus-desktop update status` and `update resolve` say what is where. Then run\n"+
			"`daedalus-desktop upgrade --rollback --data %s` again, or remove %s once everything is back",
			journal.Kind, journal.Error, journal.Kind, filepath.Join(fenceControlPath(p.Data), "retained", journal.Pre), p.Data, journalFile(p))
	}
	if journal.Work == "" {
		return fmt.Sprintf("the %s failed and its rollback could not finish: %s\n"+
			"Do not start the launcher. By hand:\n"+
			"  - the verified backup of the data is %s (data.tar.gz; manifest.json lists every file with its SHA-256)\n"+
			"  - then run `daedalus-desktop upgrade --rollback --data %s` again, or remove %s once everything is back",
			journal.Kind, journal.Error, journal.Backup, p.Data, journalFile(p))
	}
	return fmt.Sprintf("the upgrade to %s failed and its rollback could not finish: %s\n"+
		"Do not start the launcher. By hand:\n"+
		"  - the verified backup of the data is %s (data.tar.gz; manifest.json lists every file with its SHA-256)\n"+
		"  - the previous launcher's files are in %s; they belong in %s\n"+
		"  - then run `daedalus-desktop upgrade --rollback --data %s` again, or remove %s once everything is back",
		journal.To, journal.Error, journal.Backup, filepath.Join(journal.Work, "old"), journal.Root, p.Data, journalFile(p))
}

// installRoot is the folder the release's files are swapped into, and the name inside it that holds
// the launcher: Daedalus.app on macOS, the executable itself elsewhere.
func installRoot(exe string) (string, string) {
	if app, ok := bundleRoot(exe); ok {
		return filepath.Dir(app), filepath.Base(app)
	}
	return filepath.Dir(exe), filepath.Base(exe)
}

func executableIn(item string) string {
	if strings.HasSuffix(item, ".app") {
		return filepath.Join(item, "Contents", "MacOS", "daedalus-desktop")
	}
	return item
}

func isInside(child, parent string) bool {
	relative, err := filepath.Rel(parent, child)
	return err == nil && relative != ".." && !strings.HasPrefix(relative, ".."+string(os.PathSeparator)) && !filepath.IsAbs(relative)
}

// cleanOldWork removes the working folders of earlier upgrades. It is best effort: on Windows the
// previous launcher's executable may still be running out of one.
//
// Only a folder whose upgrade has ended — committed or rolled back, which leaves the marker below —
// is removed. A folder without it belongs to an upgrade that is still running, or to one that was
// cut off and still needs its old/ for a rollback; neither is anyone else's to delete.
func cleanOldWork(dir string) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return
	}
	for _, entry := range entries {
		work := filepath.Join(dir, entry.Name())
		if exists(filepath.Join(work, workFinished)) {
			_ = os.RemoveAll(work)
		}
	}
}

// workFinished marks an upgrade's working folder as no longer needed by it.
const workFinished = ".finished"

func markWorkFinished(journal *Journal) {
	if journal.Work != "" {
		_ = os.WriteFile(filepath.Join(journal.Work, workFinished), []byte(journal.Stage+"\n"), 0o600)
	}
}

// stage checks the archive against SHA256SUMS and unpacks it into staged. It returns the top-level
// names the archive holds. Nothing outside staged is touched.
func (u *Upgrader) stage(body, list []byte, source upgradeSource, staged, dataName string) ([]string, error) {
	name := source.asset
	// The signature first: with a release key in this build, SHA256SUMS must be signed by it, or
	// nothing it lists is trusted (signing.go). Without one, the checksum is all there is.
	checked, err := verifyRelease(releaseMessage(source.to, list), source.sig, trustedReleaseKeys)
	if err != nil {
		return nil, fmt.Errorf("the release was refused: %w", err)
	}
	if checked {
		u.say("SHA256SUMS is signed by the release key, for %s.", source.to)
	} else {
		u.say("SHA256SUMS is not signed (this build trusts no release key): it catches a broken download, not a forged release.")
	}
	expected := checksumFor(string(list), name)
	if expected == "" {
		return nil, fmt.Errorf("SHA256SUMS of %s does not mention %s", source.to, name)
	}
	sum := sha256.Sum256(body)
	if hex.EncodeToString(sum[:]) != expected {
		return nil, errors.New("the download does not match its checksum; not installing it")
	}
	u.say("Checksum matches.")
	if err := os.MkdirAll(staged, 0o755); err != nil {
		return nil, err
	}
	if strings.HasSuffix(name, ".zip") {
		err = unpackZip(body, staged)
	} else {
		err = unpackReleaseTar(body, staged)
	}
	if err != nil {
		return nil, fmt.Errorf("the release archive was refused: %w", err)
	}
	if err := syncStagedFolders(staged); err != nil {
		return nil, fmt.Errorf("the unpacked release could not be written to the disk: %w", err)
	}
	entries, err := os.ReadDir(staged)
	if err != nil {
		return nil, err
	}
	var items []string
	for _, entry := range entries {
		// Compared without regard to case: on APFS and NTFS "Data" is the data folder.
		for _, reserved := range []string{"data", dataName, ".daedalus-upgrade"} {
			if strings.EqualFold(entry.Name(), reserved) {
				return nil, fmt.Errorf("the release archive holds %q, which an upgrade never replaces", entry.Name())
			}
		}
		items = append(items, entry.Name())
	}
	launcher := "daedalus-desktop"
	switch {
	case strings.HasPrefix(name, "Daedalus-macOS"):
		launcher = filepath.Join("Daedalus.app", "Contents", "MacOS", "daedalus-desktop")
	case strings.Contains(name, "windows"):
		launcher = "daedalus-desktop.exe"
	}
	if info, err := os.Stat(filepath.Join(staged, launcher)); err != nil || !info.Mode().IsRegular() {
		return nil, fmt.Errorf("the release archive has no %s", filepath.ToSlash(launcher))
	}
	return items, nil
}

// checksumFor reads the line for name out of a SHA256SUMS listing ("<hex>  <name>" or with a *).
func checksumFor(list, name string) string {
	for _, line := range strings.Split(list, "\n") {
		fields := strings.Fields(line)
		if len(fields) == 2 && strings.TrimPrefix(fields[1], "*") == name && len(fields[0]) == 64 {
			return strings.ToLower(fields[0])
		}
	}
	return ""
}

// unpackLimit is the most a release may unpack to. A release is tens of megabytes; the limit is what
// keeps a small archive that expands a thousandfold from filling the disk the data folder is on,
// which the checksum alone does not rule out once a release is not the one it should be.
var unpackLimit int64 = 2 << 30

// limitedBody reads a body and counts it against what is left of the unpack limit.
type limitedBody struct {
	r    io.Reader
	left *int64
}

func (b limitedBody) Read(p []byte) (int, error) {
	n, err := b.r.Read(p)
	*b.left -= int64(n)
	if *b.left < 0 {
		return n, errUnpackTooLarge
	}
	return n, err
}

// zipEntryLimit is the most one file of a zip release may unpack to: no more than a whole release.
var zipEntryLimit int64 = assetLimit

var errUnpackTooLarge = errors.New("the release unpacks to more than a release may, in all or in one file")

// unpackReleaseTar unpacks a Linux release: files and directories only.
func unpackReleaseTar(archive []byte, dir string) error {
	zipped, err := gzip.NewReader(bytes.NewReader(archive))
	if err != nil {
		return err
	}
	defer zipped.Close()
	left := unpackLimit
	reader := tar.NewReader(limitedBody{zipped, &left})
	for {
		header, err := reader.Next()
		if errors.Is(err, io.EOF) {
			return nil
		}
		if err != nil {
			return err
		}
		raw := strings.TrimPrefix(header.Name, "./")
		if raw == "" || raw == "." {
			continue
		}
		name, err := cleanEntryName(raw)
		if err != nil {
			return err
		}
		switch header.Typeflag {
		case tar.TypeDir:
			if err := writeStagedDir(dir, name); err != nil {
				return err
			}
		case tar.TypeReg:
			if err := writeStagedFile(dir, name, os.FileMode(header.Mode).Perm(), reader); err != nil {
				return err
			}
		default:
			return fmt.Errorf("entry %q is not a file or a directory", name)
		}
	}
}

// unpackZip unpacks a macOS or Windows release. A symlink is accepted only inside the macOS bundle
// and only downwards: a relative target without a single "..", which is what a framework's
// Versions/Current and its links through it are. Checking where a ".." leads by the names alone is
// not enough — through another link the parent of a folder is somewhere else entirely — and a
// target that never goes up cannot leave the folder it is in, whatever links it passes through.
func unpackZip(archive []byte, dir string) error {
	reader, err := zip.NewReader(bytes.NewReader(archive), int64(len(archive)))
	if err != nil {
		return err
	}
	left := unpackLimit
	for _, file := range reader.File {
		name, err := cleanEntryName(file.Name)
		if err != nil {
			return err
		}
		if strings.HasPrefix(name, "__MACOSX/") || name == "__MACOSX" {
			continue
		}
		mode := file.Mode()
		switch {
		case mode.IsDir():
			if err := writeStagedDir(dir, name); err != nil {
				return err
			}
		case mode&os.ModeSymlink != 0:
			body, err := readZipEntry(file, 4096)
			if err != nil {
				return err
			}
			target := string(body)
			upward := false
			for _, part := range strings.Split(target, "/") {
				upward = upward || part == ".."
			}
			if !strings.HasPrefix(name, "Daedalus.app/") || strings.HasPrefix(target, "/") || strings.Contains(target, "\\") || target == "" || upward ||
				!strings.HasPrefix(path.Clean(path.Join(path.Dir(name), target)), "Daedalus.app/") {
				return fmt.Errorf("entry %q links to %q, which is not a link downwards inside the app bundle", name, target)
			}
			full, err := safeJoin(dir, name)
			if err != nil {
				return err
			}
			if err := ensureParents(dir, filepath.Dir(full)); err != nil {
				return err
			}
			if err := os.Symlink(target, full); err != nil {
				return err
			}
		case mode.IsRegular():
			body, err := file.Open()
			if err != nil {
				return err
			}
			// One entry larger than a whole release may be is refused, not cut short: a launcher
			// truncated at the limit would be installed as if it were whole.
			entryLeft := zipEntryLimit
			err = writeStagedFile(dir, name, mode.Perm(), limitedBody{limitedBody{body, &entryLeft}, &left})
			body.Close()
			if err != nil {
				return err
			}
		default:
			return fmt.Errorf("entry %q is not a file, a directory or a link", name)
		}
	}
	return nil
}

func readZipEntry(file *zip.File, limit int64) ([]byte, error) {
	body, err := file.Open()
	if err != nil {
		return nil, err
	}
	defer body.Close()
	return io.ReadAll(io.LimitReader(body, limit))
}

func writeStagedDir(root, name string) error {
	target, err := safeJoin(root, name)
	if err != nil {
		return err
	}
	return ensureParents(root, target)
}

func writeStagedFile(root, name string, mode os.FileMode, body io.Reader) error {
	target, err := safeJoin(root, name)
	if err != nil {
		return err
	}
	if err := ensureParents(root, filepath.Dir(target)); err != nil {
		return err
	}
	if mode == 0 {
		mode = 0o644
	}
	// O_EXCL: an archive that names one file twice, or a file where a link already is, is refused.
	file, err := os.OpenFile(target, os.O_CREATE|os.O_EXCL|os.O_WRONLY, mode)
	if err != nil {
		return err
	}
	if _, err := io.Copy(file, body); err != nil {
		file.Close()
		return err
	}
	// On the disk before it is swapped in: the rename that puts it in place is made durable, and a
	// power cut must not then leave an empty launcher under a journal that says upgraded.
	if err := launcherSync(target, file.Sync); err != nil {
		file.Close()
		return err
	}
	return file.Close()
}

// launcherSyncHook sees every sync of the launcher's files and folders during an upgrade, in order;
// a test uses it to check that each happens before the step that relies on it. Nil otherwise.
var launcherSyncHook func(path string)

// launcherSync runs one sync of the upgrade's own files through the hook.
func launcherSync(path string, sync func() error) error {
	if launcherSyncHook != nil {
		launcherSyncHook(path)
	}
	return sync()
}

// syncStagedFolders syncs every folder of the unpacked release, deepest first, for the names in it.
func syncStagedFolders(staged string) error {
	var dirs []string
	if err := filepath.WalkDir(staged, func(path string, entry os.DirEntry, err error) error {
		if err == nil && entry.IsDir() {
			dirs = append(dirs, path)
		}
		return err
	}); err != nil {
		return err
	}
	for i := len(dirs) - 1; i >= 0; i-- {
		dir := dirs[i]
		if err := launcherSync(dir, func() error { return syncDir(dir) }); err != nil {
			return err
		}
	}
	return nil
}

// syncSwapFolders makes the renames of one swapped item durable: the installation folder, where it
// now is, and old/, where the item it replaced went.
func syncSwapFolders(journal *Journal) error {
	if journal.Root == "" {
		// An update of the checkouts: no launcher files were swapped.
		return nil
	}
	for _, dir := range []string{journal.Root, filepath.Join(journal.Work, "old")} {
		if !exists(dir) {
			continue
		}
		if err := launcherSync(dir, func() error { return syncDir(dir) }); err != nil {
			return err
		}
	}
	return nil
}

// swap moves each of the release's top-level items into the installation, the old one first into
// old/. A rename is what works on every platform for an executable that is running: Windows refuses
// to overwrite or delete one, and allows it to be renamed.
//
// The journal names the item before anything about it moves, so a machine that stops between any
// two steps leaves a journal a rollback can finish from. The rollback does not trust the journal
// alone either: an item in old/ is the evidence that it was moved (unswap).
func (u *Upgrader) swap(journal *Journal) error {
	for _, item := range journal.Items {
		journal.Swapping = item
		if err := writeJournal(u.paths, journal); err != nil {
			return err
		}
		current := filepath.Join(journal.Root, item)
		if _, err := os.Lstat(current); err == nil {
			if err := os.Rename(current, filepath.Join(journal.Work, "old", item)); err != nil {
				return fmt.Errorf("could not move %s aside: %w", current, err)
			}
		}
		journal.Swapped = append(journal.Swapped, item)
		journal.Swapping = ""
		if err := writeJournal(u.paths, journal); err != nil {
			return err
		}
		if err := os.Rename(filepath.Join(journal.Work, "new", item), current); err != nil {
			return fmt.Errorf("could not put %s in place: %w", current, err)
		}
		if err := syncSwapFolders(journal); err != nil {
			return fmt.Errorf("%s could not be written to the disk in place: %w", current, err)
		}
	}
	return nil
}

// unswap undoes swap, item by item, from what is on the disk as much as from the journal: an item
// in old/ was moved there, whatever the journal managed to record, so whatever stands in its place
// goes to rejected/ and it comes back. An item the journal says was swapped and that is not in old/
// comes back from the backup. An item nothing touched is left alone. Nothing is deleted.
func unswap(journal *Journal) error {
	rejected := filepath.Join(journal.Work, "rejected")
	swapped := map[string]bool{}
	for _, item := range journal.Swapped {
		swapped[item] = true
	}
	moveAside := func(current, item string) error {
		if _, err := os.Lstat(current); err != nil {
			return nil
		}
		if err := os.MkdirAll(rejected, 0o700); err != nil {
			return err
		}
		if err := os.Rename(current, filepath.Join(rejected, item)); err != nil {
			return fmt.Errorf("could not move %s out of the way: %w", current, err)
		}
		return nil
	}
	for i := len(journal.Items) - 1; i >= 0; i-- {
		item := journal.Items[i]
		current := filepath.Join(journal.Root, item)
		old := filepath.Join(journal.Work, "old", item)
		_, currentErr := os.Lstat(current)
		switch _, oldErr := os.Lstat(old); {
		case oldErr == nil:
			if err := moveAside(current, item); err != nil {
				return err
			}
			if err := os.Rename(old, current); err != nil {
				return fmt.Errorf("could not put %s back: %w", current, err)
			}
		case swapped[item] || (item == journal.Swapping && currentErr != nil):
			// The kept copy is gone — removed by hand — or the machine stopped between the two
			// renames. What stands there now (if anything) is the release's; the original comes back
			// from the backup when it was there before the upgrade.
			if swapped[item] {
				if err := moveAside(current, item); err != nil {
					return err
				}
			}
			if journal.Backup != "" {
				if err := RestoreLauncherItem(journal.Backup, journal.Root, item); err != nil && !errors.Is(err, errNotInBackup) {
					return fmt.Errorf("could not put %s back from the backup: %w", current, err)
				}
			}
		}
	}
	journal.Swapped, journal.Swapping = nil, ""
	// The launcher from before is back only once these renames are on the disk too.
	if err := syncSwapFolders(journal); err != nil {
		return fmt.Errorf("the previous launcher's files could not be written to the disk in place: %w", err)
	}
	return nil
}

// runBinary runs the new launcher in the foreground with this terminal, and waits for it. It is a
// child rather than an exec so that the same code works on Windows, which has no exec.
//
// It is not tied to this process's context. Ctrl+C in the terminal reaches both processes; this one
// catches it (main's signal context) and keeps waiting, and the new launcher handles it itself — by
// rolling back, which must not be killed half-way by the launcher waiting for it.
func runBinary(_ context.Context, exe string, args []string, finish *InstallLock) error {
	cmd := exec.Command(exe, args...)
	cmd.Stdin, cmd.Stdout, cmd.Stderr = os.Stdin, os.Stdout, os.Stderr
	cmd.Env = os.Environ()
	if finish != nil {
		if err := passLock(cmd, finish); err != nil {
			return err
		}
	}
	return cmd.Run()
}

// inheritedFinish is the finish lock --finish runs under: the descriptor its launcher handed it,
// checked to be that lock and to carry this upgrade's token. In a test that runs both halves in one
// process, the lock is the same process's own. Anything else — no lock handed over, another
// upgrade's lock, nobody's — is a refusal: a finish that took a free lock itself would race a
// rollback that took the installation lock a moment before.
func (u *Upgrader) inheritedFinish(journal *Journal) (*InstallLock, error) {
	if u.opts.finishLock != nil {
		return u.opts.finishLock.shared(), nil
	}
	if lock, ok, err := inheritedLock(u.paths); ok || err != nil {
		if err != nil {
			return nil, err
		}
		if lock.holder.Token == "" || lock.holder.Token != journal.FinishToken {
			lock.Release()
			return nil, errors.New("the lock handed to --finish is not this upgrade's")
		}
		// The lock is this process's now as much as its parent's; say so for the next refusal.
		lock.holder.PID = os.Getpid()
		lock.rewriteHolder()
		return lock, nil
	}
	if holder, held := finishHeld(u.paths); held && holder.PID == os.Getpid() && holder.Token != "" && holder.Token == journal.FinishToken {
		return &InstallLock{holder: holder}, nil
	}
	return nil, errors.New("--finish is run by `upgrade`, which hands it the upgrade's lock; it was not handed one, so nothing was changed")
}

// appStack is the real stack, through the same App methods the buttons use.
type appStack struct{ app *App }

func (s appStack) Configured() bool { return s.app.paths.Configured() }

func (s appStack) Stop(ctx context.Context) error {
	if s.app.Native() {
		// The processes this launcher holds, if any: none for a command-line upgrade (a running
		// launcher was refused), the running agent for the page's update.
		s.app.native.Stop(ctx)
		return stopRecordedStack(ctx, s.app.paths, s.app.log)
	}
	return s.app.stop(ctx)
}

// stopRecordedStack stops the stack another launcher process started and left behind — above all
// the one a --finish that died had brought up. Its children's records name them,
// and only those whose pid, program and start time still match are stopped. Then nothing may answer
// on the app's port: whatever does is a process this launcher cannot tell apart from a stack still
// writing into the data folder, and the caller does not go on.
func stopRecordedStack(ctx context.Context, p Paths, log func(string, ...any)) error {
	if err := StopOrphans(ctx, p, log); err != nil {
		return err
	}
	if port := APIPort(p); portAnswers(port) {
		return fmt.Errorf("something still answers on the app's port %s after the stop; it may be a stack still using the data", port)
	}
	return nil
}

// Prepare fetches the published checkouts and builds their environment beside the running one.
func (s appStack) Prepare(ctx context.Context) error {
	if !s.app.Native() {
		return nil
	}
	return s.app.prepareUpdate(ctx)
}

// UpdateAndCheck is the move without a backup around it: the caller holds one.
func (s appStack) UpdateAndCheck(ctx context.Context) error {
	// What decides a commit accepts only the new stack's own answer (ready.go).
	s.app.native.strictHealth = true
	defer func() { s.app.native.strictHealth = false }()
	return s.app.update(ctx)
}

func (s appStack) Leave(ctx context.Context) { s.app.StopOnQuit() }

func (s appStack) Snapshot(ctx context.Context, backup string, manifest *Manifest) error {
	if s.app.Native() {
		return nil
	}
	return snapshotDocker(ctx, backup, manifest, dockerRunner(runDocker))
}

func (s appStack) Restore(ctx context.Context, backup string, manifest *Manifest) error {
	if s.app.Native() {
		return nil
	}
	return restoreDocker(ctx, backup, manifest, dockerRunner(runDocker))
}

func journalDescription(j *Journal, err error) string {
	if err != nil {
		return err.Error()
	}
	return "stage " + j.Stage
}
