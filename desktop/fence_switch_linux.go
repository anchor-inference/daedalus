//go:build linux

package main

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"golang.org/x/sys/unix"
)

const fenceExt4Magic = 0xEF53

type fenceOptions struct {
	Data string
	// Control is the control folder's path relative to the data folder's parent; empty means
	// .daedalus-update/<data folder's name>. It must sit beside data: trees move into it by rename,
	// which never crosses a filesystem.
	Control         string
	BackendInDocker bool
	// Held are the installation lock files the calling launcher already holds, by name (.lock, and during an
	// upgrade's finish .finish-lock). A launcher runs the switch itself, after stopping its stack,
	// and cannot let go of its lock meanwhile; taking the same lock again would be refused by flock
	// even inside one process. The switch uses these instead, after checking they are the files in
	// the data folder.
	Held map[string]*os.File
	// Handover receives, after a commit, the switch's locks on the lock files of the tree that is
	// now live. The caller swaps them for its own, which lock the retained tree by then; without the
	// swap an older launcher could take the live tree's lock. Not called on any other outcome.
	Handover func(map[string]*os.File)
	// RestoreFrom names a retained tree (pre-<op>) to put back at data instead of a fresh copy: the
	// rollback of an update whose new version did not come up. The tree must still match the
	// manifest recorded when it was retained; the data it replaces is filed as failed-<op>.
	RestoreFrom string
	// Upgrading is set by the update or upgrade that owns the in-place journal under data/upgrade:
	// that journal is unresolved while it runs, and the switch belongs to it.
	Upgrading bool
	seams     *fenceSeams
}

var errFenceSignal = errors.New("a lease-break signal arrived: a writer is waiting")

// fenceSwitch is one attempt. Its fields are the pinned identities the attempt relies on: the
// parent folder and the control folder by descriptor, each tree by its held descriptors.
type fenceSwitch struct {
	opts       fenceOptions
	seams      *fenceSeams
	rep        *fenceReport
	euid       int
	parent     *os.File
	parentKey  [2]uint64
	parentPath string
	dataName   string
	slotName   string
	slot       *os.File
	slotKey    [2]uint64
	// cDir and cName are where the second tree is: the fresh slot beside data, or in restore mode
	// the retained tree inside the control folder.
	cDir       *os.File
	cName      string
	restoreRec fenceRetainedMeta
	ctl        *fenceControl
	legacy     []*os.File
	p, c       *fenceTree
	pm         fenceManifest
	record     fenceManifest
	cCompared  bool
	legacyC    map[string]*os.File
	exchanged  bool
	sigio      uint64
	preName    string
	pKey       [2]uint64
	// recorded names the retained record written in advance of the exchange, until its tree is
	// filed under it; a switch that ends otherwise removes it.
	recorded string
	// rejectedAtSlot is a rolled-back copy a full disk kept from being filed; it is removed as a
	// slot that never went live is.
	rejectedAtSlot bool
	// needsOperator marks a switch a third party interfered with: its journal stays open, so no
	// later switch starts until someone has looked.
	needsOperator bool
}

// fencedSwitch makes the copy of opts.Data live, keeping the previous tree, or refuses. It never
// returns without a report; the report is on disk before this returns, or on standard error when
// the control folder cannot hold it.
func fencedSwitch(opts fenceOptions) fenceReport {
	var id [8]byte
	op := fmt.Sprintf("%d", time.Now().UnixNano())
	if _, err := rand.Read(id[:]); err == nil {
		op = hex.EncodeToString(id[:])
	}
	s := &fenceSwitch{opts: opts, seams: opts.seams, rep: &fenceReport{Op: op, Data: opts.Data, Outcome: fenceFailClosed, Reason: "not started", Started: time.Now().UTC()}}
	// The handler must be in place before the first lease, the self-probe's included: SIGIO's
	// default action kills the process.
	fenceSIGIO()
	func() {
		defer func() {
			// A panic is a failure like any other: it ends in a report, not in silence.
			if r := recover(); r != nil {
				s.rep.Outcome = fenceFailClosed
				s.rep.Reason = fmt.Sprintf("internal error: %v", r)
				if s.exchanged {
					s.rollback("internal error after the exchange", nil, false)
				}
			}
		}()
		s.run()
	}()
	s.finish()
	return *s.rep
}

func (s *fenceSwitch) set(outcome, reason string, entries ...fenceEntryReport) {
	s.rep.Outcome = outcome
	s.rep.Reason = reason
	s.rep.Entries = append(s.rep.Entries, entries...)
}

// fromProblem turns a fence problem into the outcome it forces, naming the writer when it can.
func (s *fenceSwitch) fromProblem(err error, fallback string) {
	// The lease-break signal first, however deeply it is wrapped: it may interrupt the measuring of
	// a file, and "cannot be measured" would then report a waiting writer as a broken fence.
	if errors.Is(err, errFenceSignal) {
		s.refuseOnSignal()
		return
	}
	var problem *fenceProblem
	if !errors.As(err, &problem) {
		s.set(fenceFailClosed, fallback+": "+err.Error())
		return
	}
	entry := fenceEntryReport{Tree: problem.Tree, Path: problem.Path, Cause: problem.Cause}
	if problem.Cause == "open for writing" && problem.Tree == "P" {
		if pids := s.writersOf(problem.Path); len(pids) > 0 {
			entry.PID = pids[0]
			entry.Cause = fmt.Sprintf("open for writing by pid %v", pids)
		} else {
			entry.Cause = "open for writing by an unknown process (another user's, or one that may not be inspected)"
		}
	}
	s.set(problem.Outcome, problem.Error(), entry)
}

func (s *fenceSwitch) writersOf(rel string) []int {
	var st unix.Stat_t
	if err := unix.Fstatat(int(s.parent.Fd()), filepath.Join(s.dataName, rel), &st, unix.AT_SYMLINK_NOFOLLOW); err != nil {
		return nil
	}
	return fenceWhoWrites(uint64(st.Dev), st.Ino)
}

func (s *fenceSwitch) abort() error {
	if fenceSIGIO() != s.sigio {
		return errFenceSignal
	}
	return nil
}

// refuseOnSignal answers a lease break before the exchange at once: the writer is blocked in open()
// until we let go, and holding it for the kernel's lease-break-time would be a 45-second freeze of
// someone else's program for nothing.
func (s *fenceSwitch) refuseOnSignal() {
	var entries []fenceEntryReport
	for _, t := range []*fenceTree{s.p, s.c} {
		if t == nil {
			continue
		}
		for _, rel := range t.brokenLeases() {
			entries = append(entries, fenceEntryReport{Tree: t.label, Path: rel, Cause: "lease broken: a writer is waiting"})
		}
	}
	s.set(fenceRefused, errFenceSignal.Error(), entries...)
}

func (s *fenceSwitch) euidNow() int {
	if s.seams != nil && s.seams.euid != nil {
		return s.seams.euid()
	}
	return os.Geteuid()
}

func (s *fenceSwitch) seamBool(f func() bool, real func() bool) bool {
	if f != nil {
		return f()
	}
	return real()
}

func (s *fenceSwitch) run() {
	seams := s.seams
	if seams == nil {
		seams = &fenceSeams{}
	}
	// Step 0: everything that can make the fence untrustworthy is checked before the first change.
	s.euid = s.euidNow()
	if s.euid == 0 {
		s.set(fenceFailClosed, "running as root: root is granted a lease on anyone's file, so a file of another user would go unnoticed")
		return
	}
	if s.seamBool(seams.container, fenceInContainer) {
		s.set(fenceFailClosed, "running inside a container: the lease fence has not been proven there")
		return
	}
	if s.opts.BackendInDocker {
		s.set(fenceFailClosed, "the agent runs in Docker: its processes write as another user, outside this fence")
		return
	}
	if s.seamBool(seams.capLease, fenceHasCapLease) {
		s.set(fenceFailClosed, "this process holds CAP_LEASE: a file of another user would be leased without complaint")
		return
	}
	if !s.seamBool(seams.leasesEnabled, fenceLeasesEnabled) {
		s.set(fenceFailClosed, "file leases are disabled on this system (fs.leases-enable is not 1)")
		return
	}
	data := filepath.Clean(s.opts.Data)
	if !filepath.IsAbs(data) {
		s.set(fenceFailClosed, "the data folder must be given as an absolute path")
		return
	}
	s.parentPath, s.dataName = filepath.Dir(data), filepath.Base(data)
	parentFD, err := unix.Open(s.parentPath, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_CLOEXEC, 0)
	if err != nil {
		s.set(fenceFailClosed, "the data folder's parent cannot be opened: "+err.Error())
		return
	}
	s.parent = os.NewFile(uintptr(parentFD), s.parentPath)
	var pst unix.Stat_t
	if err := unix.Fstat(parentFD, &pst); err != nil {
		s.set(fenceFailClosed, "stat of the parent: "+err.Error())
		return
	}
	s.parentKey = [2]uint64{uint64(pst.Dev), pst.Ino}
	var fs unix.Statfs_t
	if err := unix.Fstatfs(parentFD, &fs); err != nil || uint64(fs.Type) != fenceExt4Magic {
		s.set(fenceFailClosed, fmt.Sprintf("the filesystem (type %#x) is not one the fence has been proven on; only ext4 is", uint64(fs.Type)))
		return
	}
	parentMount, err := fenceMountID(parentFD)
	if err != nil {
		s.set(fenceFailClosed, "mount identity unavailable: "+err.Error())
		return
	}
	if kind, err := fenceMountFSType(parentMount); err != nil || kind != "ext4" {
		s.set(fenceFailClosed, fmt.Sprintf("the filesystem is %q, not ext4: %v", kind, err))
		return
	}
	var dst unix.Stat_t
	if err := unix.Fstatat(parentFD, s.dataName, &dst, unix.AT_SYMLINK_NOFOLLOW); err != nil {
		s.set(fenceFailClosed, "the data folder cannot be found: "+err.Error())
		return
	}
	if dst.Mode&unix.S_IFMT != unix.S_IFDIR {
		s.set(fenceFailClosed, "the data folder is not a directory (a symlink is not followed)")
		return
	}
	s.pKey = [2]uint64{uint64(dst.Dev), dst.Ino}
	if dst.Dev != pst.Dev {
		s.set(fenceFailClosed, "the data folder is a mount of its own; it cannot be exchanged with a copy beside it")
		return
	}
	ctl, problem := fenceOpenControl(s.parent, s.controlName(), uint32(s.euid), uint64(pst.Dev), seams)
	if problem != "" {
		s.set(fenceFailClosed, problem)
		return
	}
	s.ctl = ctl
	if unfinished := ctl.unfinished(); unfinished != "" {
		s.set(fenceFailClosed, unfinished)
		return
	}
	// The runtime is a cache with thousands of hard links into uv's own, and it lives outside the
	// data folder now. One still inside means a launcher from before the move, or one that put it
	// back; either way it is not copied, fenced or skipped silently.
	if _, err := os.Lstat(filepath.Join(data, "runtime")); err == nil {
		s.set(fenceFailClosed, "the data folder still holds runtime/, the downloaded interpreter and environments; start this launcher once — it moves them out of the data folder — then switch again")
		return
	}
	// An upgrade (the in-place kind) that stopped half way is only read, never resolved here.
	if paths, err := NewPaths(data); err == nil && !s.opts.Upgrading {
		if journal, err := readJournal(paths); err == nil && journal.unresolved() {
			s.set(fenceFailClosed, fmt.Sprintf("an earlier upgrade stopped at stage %s; run `upgrade --rollback` first", journal.Stage))
			return
		} else if err != nil && !errors.Is(err, os.ErrNotExist) {
			s.set(fenceFailClosed, "the earlier upgrade's journal cannot be read: "+err.Error())
			return
		}
	}
	if busy := ctl.lock(); busy != "" {
		s.set(fenceRefused, busy)
		return
	}
	if err := s.journal("started"); err != nil {
		s.set(fenceFailClosed, "the switch journal cannot be written, so nothing was changed: "+err.Error())
		return
	}
	// Legacy locks first, before any lease: a running launcher answers here at once, instead of
	// its log file costing a lease refusal, and nothing has to wait for a lease break.
	if refusal := s.takeLegacyLocks(s.parent, s.dataName, "P"); refusal != "" {
		s.set(fenceRefused, refusal)
		return
	}
	count, err := fenceCountTree("P", s.parent, s.dataName, uint32(s.euid), uint64(pst.Dev), seams)
	if err != nil {
		s.fromProblem(err, "capacity check")
		return
	}
	if err := fenceRaiseNoFile(count); err != nil {
		s.set(fenceFailClosed, err.Error())
		return
	}
	limits, err := fenceWatchBudget(count)
	s.rep.Limits = append(s.rep.Limits, limits...)
	if err != nil {
		s.set(fenceFailClosed, err.Error())
		return
	}
	if reason := fenceSelfProbe(ctl, s.rep.Op, seams, &s.rep.LeasesTaken); reason != "" {
		s.set(fenceFailClosed, reason)
		return
	}
	if s.opts.RestoreFrom != "" {
		meta, err := ctl.readMeta(s.opts.RestoreFrom)
		if err != nil || meta.Kind != "pre" || len(meta.Manifest) == 0 {
			s.set(fenceFailClosed, fmt.Sprintf("%s is not a recorded copy of the data from before an update: %v", s.opts.RestoreFrom, err))
			return
		}
		var st unix.Stat_t
		if err := unix.Fstatat(int(ctl.sub["retained"].Fd()), s.opts.RestoreFrom, &st, unix.AT_SYMLINK_NOFOLLOW); err != nil {
			s.set(fenceFailClosed, "the kept copy cannot be found: "+err.Error())
			return
		}
		s.restoreRec, s.cDir, s.cName = meta, ctl.sub["retained"], s.opts.RestoreFrom
		s.slotKey = [2]uint64{uint64(st.Dev), st.Ino}
	} else {
		if reason := s.makeSlot(seams); reason != "" {
			s.set(fenceFailClosed, reason)
			return
		}
		s.cDir, s.cName = s.parent, s.slotName
	}
	if err := s.journal("fencing"); err != nil {
		s.set(fenceFailClosed, "the switch journal cannot be written, so nothing was changed: "+err.Error())
		return
	}

	s.sigio = fenceSIGIO()
	p, err := fenceOpenTree("P", s.parent, s.dataName, s.euid, seams, &s.rep.LeasesTaken)
	if err != nil {
		s.fromProblem(err, "fencing the data folder")
		return
	}
	s.p = p
	s.pm, err = p.manifest(s.abort)
	if err != nil {
		s.fromProblem(err, "measuring the data folder")
		return
	}
	if s.opts.RestoreFrom == "" {
		if reason := s.checkSpace(seams, &fs); reason != "" {
			s.set(fenceRefused, reason)
			return
		}
		s.record, err = fenceCopyTree(p, s.pm, s.slot, s.abort, seams)
		if err != nil {
			s.fromProblem(err, "copying")
			return
		}
	}
	seams.call(seams.beforeCFence)
	if refusal := s.takeLegacyLocks(s.cDir, s.cName, "C"); refusal != "" {
		s.set(fenceRefused, refusal)
		return
	}
	c, err := fenceOpenTree("C", s.cDir, s.cName, s.euid, seams, &s.rep.LeasesTaken)
	if err != nil {
		s.fromProblem(err, "fencing the copy")
		if s.rep.Outcome == fenceFailClosed {
			// Our own fresh copy failing a check the original passed means someone reached it.
			s.rep.Outcome = fenceRefused
		}
		return
	}
	s.c = c
	if seams.afterCFence != nil {
		seams.afterCFence(c)
	}
	// The copy is compared only now, under its own fence: a change made to it before the fence was
	// up is in this comparison, and one made after is a lease break or an event.
	cm, err := c.manifest(s.abort)
	if err != nil {
		s.fromProblem(err, "measuring the copy")
		return
	}
	want, outcome, reason := s.pm, fenceRefused, "copy differs"
	if s.opts.RestoreFrom != "" {
		// The kept copy must be exactly what was recorded when it was kept. One that changed since
		// holds something nobody has looked at, and is not put back without the operator.
		want, outcome, reason = s.restoreRec.Manifest, fenceFailClosed, "the kept copy changed since it was recorded; it is not put back automatically"
	}
	if diffs := fenceManifestDiff(want, cm); len(diffs) > 0 {
		var entries []fenceEntryReport
		for _, diff := range diffs {
			entries = append(entries, fenceEntryReport{Tree: "C", Path: diff, Cause: "differs from what it must be"})
		}
		s.set(outcome, reason, entries...)
		return
	}
	s.cCompared = true
	if seams.afterCompare != nil {
		seams.afterCompare(p, c)
	}

	// Sweep 1: nothing may have happened to either tree since it was fenced.
	sweep := s.sweepBoth(nil, nil)
	if fenceSweepFails(sweep) {
		s.rep.Overflow = s.rep.Overflow || sweep.Overflow
		outcome := fenceRefused
		if fenceMoved(sweep) {
			// Our folders are not where the configuration says: that is not a writer to wait out.
			outcome = fenceFailClosed
		}
		s.set(outcome, "writer activity before the exchange"+fenceOverflowNote(sweep), sweep.Entries...)
		return
	}
	if seams.afterSweep1 != nil {
		seams.afterSweep1(p, c)
	}
	if fenceSameNamed(p.root()) != nil || fenceSameNamed(c.root()) != nil {
		s.set(fenceFailClosed, "the trees were renamed before the exchange")
		return
	}
	// The copy goes to the disk before it can become live. Without this a power cut in the half
	// minute after a switch — before the kernel writes the copy's pages back on its own — leaves a
	// live data folder of empty files under a journal that says committed, and the one whole copy
	// filed for automatic removal. syncfs and not an fsync per file: one call, and it also covers
	// the directories the copy made. A restore needs it as much: the kept copy was written by a
	// switch long ago, but nothing since has made sure it reached the disk.
	if err := s.durable("copy", func() error { return unix.Syncfs(int(s.cDir.Fd())) }); err != nil {
		s.set(fenceFailClosed, "the copy could not be written to the disk, so nothing was switched: "+err.Error())
		return
	}
	// The record of the tree data will leave is written now, while nothing has been exchanged: after
	// the exchange a full disk could refuse it, and a tree filed without a record is one nobody can
	// restore or remove.
	pending := s.retainedRecord()
	if err := s.ctl.writeMeta(pending); err != nil {
		s.set(fenceRefused, "the record of the data from before cannot be written, so nothing was switched: "+err.Error())
		return
	}
	s.recorded = pending.Name
	if err := s.journal("exchanging"); err != nil {
		s.set(fenceFailClosed, "the switch journal cannot be written, so nothing was switched: "+err.Error())
		return
	}
	if err := unix.Renameat2(int(s.parent.Fd()), s.dataName, int(s.cDir.Fd()), s.cName, unix.RENAME_EXCHANGE); err != nil {
		s.set(fenceFailClosed, "the exchange failed; nothing was switched: "+err.Error())
		return
	}
	s.exchanged = true
	p.rebind(s.cDir, s.cName)
	c.rebind(s.parent, s.dataName)
	// The exchange itself, in both folders it changed, before anything is decided on it.
	if err := s.durable("exchange", s.syncFolders); err != nil {
		s.rollback("the exchange could not be written to the disk: "+err.Error(), nil, false)
		return
	}
	s.journalAfter("exchanged")
	if seams.afterExchange != nil {
		seams.afterExchange(p, c)
	}

	// Sweep 2: exactly one MOVE_SELF per root is ours; anything else is someone else's.
	pLedger, cLedger := &fenceLedger{rootMoves: 1}, &fenceLedger{rootMoves: 1}
	sweep = s.sweepBoth(pLedger, cLedger)
	if pLedger.rootMoves > 0 || cLedger.rootMoves > 0 {
		sweep.add("", ".", "the exchange's own event is missing")
	}
	if fenceSweepFails(sweep) {
		s.rollback("writer activity during the exchange"+fenceOverflowNote(sweep), sweep.Entries, sweep.Overflow)
		return
	}
	if seams.afterSweep2 != nil {
		seams.afterSweep2(p, c)
	}
	refs, unscanned, procLimits := fenceScanProcs(p.inodes())
	s.rep.Unscanned = append(s.rep.Unscanned, unscanned...)
	s.rep.Limits = append(s.rep.Limits, procLimits...)
	if len(refs) > 0 {
		var entries []fenceEntryReport
		for _, ref := range refs {
			entries = append(entries, fenceEntryReport{Tree: "P", Path: ref.Path, Cause: "a process is working in the previous tree (" + ref.What + ")", PID: ref.PID, TID: ref.TID})
		}
		s.rollback("a process is working in the previous tree", entries, false)
		return
	}
	// The final sweep: a writer who arrived after sweep 2 is still blocked in open(), and a rollback
	// now puts its write into live data.
	sweep = s.sweepBoth(nil, nil)
	if fenceSweepFails(sweep) {
		s.rollback("writer activity before the commit"+fenceOverflowNote(sweep), sweep.Entries, sweep.Overflow)
		return
	}
	if seams.afterFinalSweep != nil {
		seams.afterFinalSweep(p, c)
	}
	s.journalAfter("committing")
	name, err := s.retain(p, pending, true)
	if errors.Is(err, errFenceNotDurable) {
		// Filed and recorded; only the sync of the folders failed. The switch stands, and says so.
		s.rep.Limits = append(s.rep.Limits, err.Error())
		name, err = pending.Name, nil
		s.rep.Retained = append(s.rep.Retained, fenceRetained{Path: filepath.Join(s.ctl.path, "retained", name), Kind: pending.Kind, GC: pending.GC, Reason: pending.Reason})
	}
	if err != nil {
		s.foreignExchange("the previous tree was not where the exchange left it: " + err.Error())
		return
	}
	s.recorded = ""
	s.preName = name
	if s.opts.RestoreFrom != "" {
		s.ctl.forget(s.opts.RestoreFrom)
		s.set(fenceCommitted, "the data from before the update is live again; what replaced it is kept")
		return
	}
	s.set(fenceCommitted, "the copy is live; the previous tree is kept")
}

// retainedRecord is the record of the tree the exchange moves away from data: the data from
// before, removable once a later update has committed; or, for a restore, the data the version that
// did not come up left, kept for the operator — it may hold what the agent did under that version,
// and nothing decides that for them.
func (s *fenceSwitch) retainedRecord() fenceRetainedMeta {
	if s.opts.RestoreFrom != "" {
		return s.recordOf("failed", "manual", "the data as the version that did not come up left it")
	}
	return s.recordOf("pre", "auto", "")
}

// syncFolders makes a rename between the parent and the second tree's folder durable.
func (s *fenceSwitch) syncFolders() error {
	if err := s.parent.Sync(); err != nil {
		return err
	}
	if s.cDir != s.parent {
		return s.cDir.Sync()
	}
	return nil
}

// durable runs one step that makes the switch survive a power cut, through the test seam first.
func (s *fenceSwitch) durable(what string, do func() error) error {
	if s.seams != nil && s.seams.sync != nil {
		if err := s.seams.sync(what); err != nil {
			return err
		}
	}
	return do()
}

// fenceSpaceMargin is what must stay free beyond the copy itself: the app writes its database and
// logs on the same disk the moment it starts, and a disk the switch filled to the last block is
// one it cannot even record its own outcome on.
var fenceSpaceMargin uint64 = 512 << 20

// checkSpace refuses a copy the filesystem has no room for. The copy costs more than the tree
// occupies: every file is written out whole (a sparse file becomes dense) and each name of a file
// with two names in the tree becomes a file of its own. The data from before stays until a later
// update, so at the peak the disk holds it, the copy and the kept copy of the update before.
func (s *fenceSwitch) checkSpace(seams *fenceSeams, fs *unix.Statfs_t) string {
	block := uint64(fs.Bsize)
	if block == 0 {
		block = 4096
	}
	var need uint64
	for _, entry := range s.pm {
		need += block
		if entry.Kind == "file" && entry.Size > 0 {
			need += (uint64(entry.Size) + block - 1) / block * block
		}
	}
	var st unix.Statfs_t
	if err := unix.Fstatfs(int(s.parent.Fd()), &st); err != nil {
		return "the free space cannot be read: " + err.Error()
	}
	free := st.Bavail * uint64(st.Bsize)
	if seams.freeSpace != nil {
		free = seams.freeSpace()
	}
	margin := fenceSpaceMargin
	if need/10 > margin {
		margin = need / 10
	}
	if free < need+margin {
		return fmt.Sprintf("not enough free space for the copy of the data folder: it needs %s, and %s more must stay free, while %s is free; free some space and try again", fenceBytes(need), fenceBytes(margin), fenceBytes(free))
	}
	return ""
}

func fenceBytes(n uint64) string {
	switch {
	case n >= 1<<30:
		return fmt.Sprintf("%.1f GiB", float64(n)/(1<<30))
	case n >= 1<<20:
		return fmt.Sprintf("%.1f MiB", float64(n)/(1<<20))
	}
	return fmt.Sprintf("%d KiB", (n+1023)/1024)
}

func fenceMoved(s fenceSweepResult) bool {
	for _, entry := range s.Entries {
		if entry.Tree == "parent" || entry.Tree == "control" {
			return true
		}
	}
	return false
}

func fenceOverflowNote(s fenceSweepResult) string {
	if s.Overflow {
		return " (inotify overflow: the observation is void)"
	}
	return ""
}

func (s *fenceSwitch) controlName() string {
	if s.opts.Control != "" {
		return s.opts.Control
	}
	return fenceControlName(s.opts.Data)
}

// sweepBoth sweeps P and C and checks everything that is not in either tree but must not move:
// the parent folder and the control folder by path against their pinned descriptors (nothing
// watches the parent, so this is how its rename is seen), and the lease-break signal.
func (s *fenceSwitch) sweepBoth(pLedger, cLedger *fenceLedger) fenceSweepResult {
	var result fenceSweepResult
	result.merge(s.p.sweep(pLedger))
	result.merge(s.c.sweep(cLedger))
	if key, err := fenceStatIno(s.parentPath); err != nil || key != s.parentKey {
		result.add("parent", s.parentPath, "the data folder's parent was renamed or replaced")
	}
	if key, err := fenceStatIno(filepath.Join(s.parentPath, s.controlName())); err != nil || key != s.ctl.key {
		result.add("control", s.controlName(), "the control folder was renamed or replaced")
	}
	if now := fenceSIGIO(); now != s.sigio {
		result.add("", "", "a lease-break signal arrived")
		s.sigio = now
	}
	return result
}

// makeSlot creates the slot, private by an explicit chmod and a check of the result: a mode that
// merely came from the umask is not a guarantee.
func (s *fenceSwitch) makeSlot(seams *fenceSeams) string {
	s.slotName = "." + s.dataName + "-slot-" + s.rep.Op
	fd, reason := fenceMkdirPrivate(s.parent, s.slotName, uint32(s.euid), seams)
	if reason != "" {
		return reason
	}
	s.slot = os.NewFile(uintptr(fd), s.slotName)
	var st unix.Stat_t
	if err := unix.Fstat(fd, &st); err != nil {
		return "stat of the slot: " + err.Error()
	}
	s.slotKey = [2]uint64{uint64(st.Dev), st.Ino}
	return ""
}

func fenceMkdirPrivate(parent *os.File, name string, euid uint32, seams *fenceSeams) (int, string) {
	if err := unix.Mkdirat(int(parent.Fd()), name, 0o700); err != nil && !errors.Is(err, unix.EEXIST) {
		return -1, fmt.Sprintf("%s cannot be created: %v", name, err)
	}
	fd, err := unix.Openat(int(parent.Fd()), name, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	if err != nil {
		return -1, fmt.Sprintf("%s cannot be opened: %v", name, err)
	}
	var st unix.Stat_t
	if err := unix.Fstat(fd, &st); err != nil || st.Uid != euid {
		unix.Close(fd)
		return -1, fmt.Sprintf("%s is not owned by this user", name)
	}
	if err := unix.Fchmod(fd, 0o700); err != nil {
		unix.Close(fd)
		return -1, fmt.Sprintf("%s cannot be made private: %v", name, err)
	}
	if err := unix.Fstat(fd, &st); err != nil {
		unix.Close(fd)
		return -1, fmt.Sprintf("stat of %s: %v", name, err)
	}
	mode := st.Mode & 0o7777
	if seams != nil && seams.privateMode != nil {
		mode = seams.privateMode(name, mode)
	}
	if mode != 0o700 {
		unix.Close(fd)
		return -1, fmt.Sprintf("%s has mode %#o, not 0700", name, mode)
	}
	return fd, ""
}

// takeLegacyLocks holds the launcher's installation and finish locks of one tree, if they exist, through a
// read-only descriptor. flock excludes a second holder whatever its open mode, and a read-only
// descriptor leaves the read lease grantable; opened for writing, our own lock would refuse our
// own fence. A lock someone holds refuses the switch at once. A missing lock is not created.
func (s *fenceSwitch) takeLegacyLocks(dir *os.File, root, tree string) string {
	rootFD, err := unix.Openat(int(dir.Fd()), root, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	if err != nil {
		return ""
	}
	defer unix.Close(rootFD)
	upgradeFD, err := unix.Openat(rootFD, "upgrade", unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	if err != nil {
		return ""
	}
	defer unix.Close(upgradeFD)
	for _, name := range []string{lockName, finishLockName} {
		fd, err := unix.Openat(upgradeFD, name, unix.O_RDONLY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
		if err != nil {
			continue
		}
		if held := s.opts.Held[name]; held != nil && tree == "P" && fenceSameFile(held, fd) {
			// The caller's own lock on this very file: already exclusive, and ours to keep using.
			unix.Close(fd)
			continue
		}
		if err := unix.Flock(fd, unix.LOCK_EX|unix.LOCK_NB); err != nil {
			unix.Close(fd)
			holder, ok := readHolderAt(filepath.Join(dir.Name(), root, "upgrade", name))
			who := describeHolder(holder, ok)
			if tree == "C" {
				return "someone holds the lock inside our private copy: " + who
			}
			return "a launcher or an upgrade of this installation is running — " + who + "; close it and try again"
		}
		file := os.NewFile(uintptr(fd), tree+" "+name)
		s.legacy = append(s.legacy, file)
		if tree == "C" {
			if s.legacyC == nil {
				s.legacyC = make(map[string]*os.File)
			}
			s.legacyC[name] = file
		}
	}
	return ""
}

func fenceSameFile(file *os.File, fd int) bool {
	var a, b unix.Stat_t
	return unix.Fstat(int(file.Fd()), &a) == nil && unix.Fstat(fd, &b) == nil && a.Dev == b.Dev && a.Ino == b.Ino
}

// retain moves a tree of ours into the control folder, checking by inode before and after: a name
// alone could by now lead to someone else's tree, and nothing foreign may ever be filed as ours.
func (s *fenceSwitch) retain(t *fenceTree, meta fenceRetainedMeta, recorded bool) (string, error) {
	if err := s.ctl.adopt(t, meta, recorded); err != nil {
		return "", err
	}
	s.rep.Retained = append(s.rep.Retained, fenceRetained{Path: filepath.Join(s.ctl.path, "retained", meta.Name), Kind: meta.Kind, GC: meta.GC, Reason: meta.Reason})
	return meta.Name, nil
}

// recordOf is the record of a tree this switch files, with the manifest measured under the fence.
func (s *fenceSwitch) recordOf(kind, gc, reason string) fenceRetainedMeta {
	return fenceRetainedMeta{Name: kind + "-" + s.rep.Op, Kind: kind, Op: s.rep.Op, GC: gc, Reason: reason, Created: time.Now().UTC(), Manifest: s.pm}
}

// rollback exchanges back through the pinned parent descriptor, and only after checking by inode
// that data holds our copy and the slot our original; afterwards it checks that the reverse is so.
// Any other arrangement means a third party exchanged or renamed, and the switch stops without
// touching data again.
func (s *fenceSwitch) rollback(reason string, entries []fenceEntryReport, overflow bool) {
	s.rep.Overflow = s.rep.Overflow || overflow
	s.set(fenceRolledBack, reason, entries...)
	if fenceSameNamed(s.c.root()) != nil || fenceSameNamed(s.p.root()) != nil {
		s.foreignExchange(reason + "; and before the rollback the trees were not where the exchange left them")
		return
	}
	if err := unix.Renameat2(int(s.parent.Fd()), s.dataName, int(s.cDir.Fd()), s.cName, unix.RENAME_EXCHANGE); err != nil {
		s.foreignExchange(reason + "; and the rollback exchange failed: " + err.Error())
		return
	}
	s.p.rebind(s.parent, s.dataName)
	s.c.rebind(s.cDir, s.cName)
	if fenceSameNamed(s.p.root()) != nil || fenceSameNamed(s.c.root()) != nil {
		s.foreignExchange(reason + "; and after the rollback the trees were not where it put them")
		return
	}
	s.exchanged = false
	if err := s.durable("exchange", s.syncFolders); err != nil {
		s.rep.Limits = append(s.rep.Limits, "the rollback's exchange could not be written to the disk: "+err.Error())
	}
	s.dropPending()
	s.journalAfter("rolled-back")
	if s.opts.RestoreFrom != "" {
		// The kept copy goes back to where it was kept, unchanged in name and record.
		s.set(fenceRolledBack, reason)
		return
	}
	if _, err := s.retain(s.c, s.recordOf("rejected", "auto", reason), false); err != nil {
		if errors.Is(err, errFenceNotRecorded) {
			// A full disk: the copy stays at the slot, and the end of the switch removes it the way
			// it removes a slot that never went live, with no record to write first.
			s.rejectedAtSlot = true
			s.set(fenceRolledBack, reason)
			return
		}
		s.foreignExchange(reason + "; and the rejected copy was not at the slot: " + err.Error())
		return
	}
	s.set(fenceRolledBack, reason)
}

// dropPending removes the record written in advance for a tree that will not be filed after all.
func (s *fenceSwitch) dropPending() {
	if s.recorded != "" {
		s.ctl.forget(s.recorded)
		s.recorded = ""
	}
}

// foreignExchange is the stop for a third party that exchanged or renamed our trees. Nothing is
// exchanged any more. The tree at data stays live whatever it is; each of our trees found where we
// expect it is filed for the operator, never for automatic removal.
func (s *fenceSwitch) foreignExchange(reason string) {
	s.set(fenceFailClosed, "someone else exchanged or renamed the trees: "+reason)
	s.needsOperator = true
	s.dropPending()
	s.journalAfter("needs-operator")
	at := func(dir *os.File, name string) string {
		var st unix.Stat_t
		if err := unix.Fstatat(int(dir.Fd()), name, &st, unix.AT_SYMLINK_NOFOLLOW); err != nil {
			return "nothing"
		}
		key := [2]uint64{uint64(st.Dev), st.Ino}
		switch key {
		case [2]uint64{s.p.root().dev, s.p.root().ino}:
			return "P"
		case [2]uint64{s.c.root().dev, s.c.root().ino}:
			return "C"
		}
		return "a foreign tree"
	}
	atData, atSlot := at(s.parent, s.dataName), at(s.cDir, s.cName)
	s.rep.Live = atData
	s.rep.Entries = append(s.rep.Entries,
		fenceEntryReport{Tree: atData, Path: s.dataName, Cause: "found at data"},
		fenceEntryReport{Tree: atSlot, Path: s.cName, Cause: "found where the second tree belongs"})
	for _, t := range []*fenceTree{s.p, s.c} {
		label := t.label
		switch {
		case atData == label:
			if label == "C" {
				s.rep.Retained = append(s.rep.Retained, fenceRetained{Path: filepath.Join(s.parentPath, s.dataName), Kind: "live-unverified", GC: "manual", Reason: "the copy is live without the final checks"})
			}
		case atSlot == label && s.opts.RestoreFrom != "" && label == "C":
			// The kept copy is where it was kept; it only stops being removable.
			s.ctl.setMeta(s.cName, func(m *fenceRetainedMeta) { m.GC = "manual"; m.Reason = "a restore was interfered with" })
		case atSlot == label:
			t.rebind(s.cDir, s.cName)
			kind := "rejected"
			if label == "P" {
				kind = "pre"
				if s.opts.RestoreFrom != "" {
					kind = "failed"
				}
			}
			if _, err := s.retain(t, s.recordOf(kind, "manual", "moved by someone else during the switch"), false); err != nil {
				s.rep.Retained = append(s.rep.Retained, fenceRetained{Path: filepath.Join(s.cDir.Name(), s.cName), Kind: kind, GC: "manual", Reason: "left in place: " + err.Error()})
			}
		default:
			s.rep.Retained = append(s.rep.Retained, fenceRetained{Path: "unknown", Kind: label, GC: "manual", Reason: "not found at data or at the slot"})
		}
	}
}

func (s *fenceSwitch) journal(phase string) error {
	if s.ctl == nil {
		return nil
	}
	return s.ctl.journal(fenceJournal{Op: s.rep.Op, Phase: phase, Data: s.opts.Data, Slot: s.cName, SlotRetained: s.opts.RestoreFrom != "", P: s.pKey, C: s.slotKey})
}

// journalAfter records a phase after the exchange. There is no going back on a write that fails
// there — the entry before it names both trees by inode, which is all `update resolve` needs — so
// the failure is only reported.
func (s *fenceSwitch) journalAfter(phase string) {
	if err := s.journal(phase); err != nil {
		s.rep.Limits = append(s.rep.Limits, fmt.Sprintf("the switch journal could not record %q: %v", phase, err))
	}
}

// finish releases everything in the one order that keeps the installation lock meaningful: the report
// first, then the leases, then the legacy flocks, then our own locks. Between the leases and the
// flocks a launcher that tries its lock gets errLocked, never the lock.
func (s *fenceSwitch) finish() {
	if s.rep.Live == "" {
		s.rep.Live = "P"
		if s.exchanged {
			s.rep.Live = "C"
		}
	}
	s.rep.ExitCode = fenceExitCode(s.rep.Outcome)
	s.writeReport()
	if s.seams != nil && s.seams.beforeRelease != nil && s.p != nil && s.c != nil {
		s.seams.beforeRelease(s.p, s.c)
	}
	if s.rep.Outcome == fenceCommitted {
		preLate, preOverflow := s.p.releaseChecked(&fenceLedger{rootMoves: 1})
		liveLate, liveOverflow := s.c.releaseChecked(nil)
		if preOverflow {
			preLate = append(preLate, "(inotify overflow)")
		}
		if liveOverflow {
			liveLate = append(liveLate, "(inotify overflow)")
		}
		if len(preLate) > 0 {
			s.rep.LateWritePossible = append(s.rep.LateWritePossible, fenceLateWrite{Tree: "pre", Paths: preLate})
			s.ctl.markManual(s.preName, preLate)
			for i := range s.rep.Retained {
				if s.rep.Retained[i].Kind == "pre" {
					s.rep.Retained[i].GC = "manual"
					s.rep.Retained[i].Reason = "a late write may have reached it"
				}
			}
		}
		if len(liveLate) > 0 {
			s.rep.LateWritePossible = append(s.rep.LateWritePossible, fenceLateWrite{Tree: "live", Paths: liveLate})
		}
		if len(preLate)+len(liveLate) > 0 {
			s.writeReport()
		}
	} else {
		s.p.close()
		s.c.close()
	}
	if s.seams != nil && s.seams.afterLeaseRelease != nil {
		s.seams.afterLeaseRelease()
	}
	s.collectSlot()
	if s.rep.Outcome == fenceCommitted && s.opts.Handover != nil && len(s.legacyC) > 0 {
		// The live tree's locks go to the caller instead of being released: they are what keeps
		// an older launcher off the data from here on.
		kept := s.legacy[:0]
		for _, file := range s.legacy {
			handed := false
			for _, c := range s.legacyC {
				handed = handed || c == file
			}
			if !handed {
				kept = append(kept, file)
			}
		}
		s.legacy = kept
		s.opts.Handover(s.legacyC)
	}
	for _, file := range s.legacy {
		_ = unix.Flock(int(file.Fd()), unix.LOCK_UN)
		_ = file.Close()
	}
	s.legacy = nil
	if s.slot != nil {
		_ = s.slot.Close()
	}
	if s.ctl != nil {
		s.dropPending()
		if !s.needsOperator {
			s.journalAfter("done")
		}
		s.ctl.unlock()
		s.ctl.close()
	}
	if s.parent != nil {
		_ = s.parent.Close()
	}
}

// collectSlot handles a slot that was never exchanged. It is ours, but it is removed only the way
// any retained tree is — under a fresh fence and after a full comparison with what we know we put
// there. Anything else keeps it.
func (s *fenceSwitch) collectSlot() {
	if s.slot == nil || s.exchanged || s.rep.Outcome == fenceCommitted || (s.rep.Outcome == fenceRolledBack && !s.rejectedAtSlot) || s.needsOperator {
		return
	}
	expected := s.record
	if s.cCompared {
		expected = s.pm
	}
	if expected == nil {
		expected = make(fenceManifest)
	}
	if _, ok := expected["."]; !ok {
		if entry, err := fenceFDEntry(s.slot, "dir", nil); err == nil {
			expected["."] = entry
		}
	}
	// Removed straight from where it is, with its record kept in memory: the record goes to the disk
	// only if the slot stays. A copy cut off by a full disk is the very thing that must go to give
	// the space back, and a record written first would need space the disk no longer has.
	meta := fenceRetainedMeta{Name: "slot-" + s.rep.Op, Kind: "slot", Op: s.rep.Op, GC: "auto", Reason: "left by a switch that did not exchange", Created: time.Now().UTC(), Manifest: expected}
	gc := fenceGCReport{Name: meta.Name, Outcome: fenceRetainedGC, At: time.Now().UTC()}
	s.ctl.remove(s.parent, s.slotName, meta, s.euid, s.seams, &gc)
	s.ctl.writeGC(&gc)
	s.rep.GC = append(s.rep.GC, gc)
	if gc.Outcome != fenceDeleted {
		slotNode := &fenceNode{rel: ".", kind: "dir", file: s.slot, parent: s.parent, name: s.slotName, dev: s.slotKey[0], ino: s.slotKey[1]}
		holder := &fenceTree{label: "slot", dirs: []*fenceNode{slotNode}}
		where := filepath.Join(s.ctl.path, "retained", meta.Name)
		if fenceSameNamed(slotNode) == nil {
			// Still where it was made: the removal never moved it. Filed for the operator.
			meta.GC, meta.Reason = "manual", gc.Reason
			if err := s.ctl.adopt(holder, meta, false); err != nil {
				where = filepath.Join(s.parentPath, s.slotName)
				gc.Reason += "; left in place: " + err.Error()
			}
		}
		s.rep.Retained = append(s.rep.Retained, fenceRetained{Path: where, Kind: "slot", GC: "manual", Reason: gc.Reason})
	}
	s.writeReport()
}

func (s *fenceSwitch) writeReport() {
	s.rep.Finished = time.Now().UTC()
	s.rep.DurationMS = s.rep.Finished.Sub(s.rep.Started).Milliseconds()
	s.rep.ExitCode = fenceExitCode(s.rep.Outcome)
	body, err := json.MarshalIndent(s.rep, "", "  ")
	if err == nil && s.ctl != nil {
		if err = s.ctl.writeFile("reports", s.rep.Op+".json", body); err == nil {
			s.rep.ReportPath = filepath.Join(s.ctl.path, "reports", s.rep.Op+".json")
			return
		}
	}
	// No control folder to hold it: the report still exists, on standard error, and the exit code
	// says the data was not switched.
	fmt.Fprintf(os.Stderr, "fenced switch report (not written to disk: %v):\n%s\n", err, body)
}

// fencePlatform is the part of step 0 that says whether the fence can be had here at all, without
// creating or opening anything of the data folder's: the platform and filesystem checks. What the
// data itself holds (a writer, a hard link, a file of another user) is for the switch to find, and
// its refusal is final.
func fencePlatform(data string) error {
	switch {
	case os.Geteuid() == 0:
		return errors.New("running as root")
	case fenceInContainer():
		return errors.New("running inside a container")
	case fenceHasCapLease():
		return errors.New("this process holds CAP_LEASE")
	case !fenceLeasesEnabled():
		return errors.New("file leases are disabled on this system")
	}
	parent := filepath.Dir(filepath.Clean(data))
	fd, err := unix.Open(parent, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_CLOEXEC, 0)
	if err != nil {
		return err
	}
	defer unix.Close(fd)
	// The data folder itself, not followed: the switch exchanges it beside its parent's other
	// entries, so a symlink to elsewhere, or a folder that is a mount of its own, is one it could
	// only ever refuse — every update would stop there with no way through.
	var pst, dst unix.Stat_t
	if err := unix.Fstat(fd, &pst); err != nil {
		return err
	}
	if err := unix.Fstatat(fd, filepath.Base(filepath.Clean(data)), &dst, unix.AT_SYMLINK_NOFOLLOW); err == nil {
		switch {
		case dst.Mode&unix.S_IFMT == unix.S_IFLNK:
			return errors.New("the data folder is a symbolic link")
		case dst.Dev != pst.Dev:
			return errors.New("the data folder is a mount of its own")
		}
	}
	var fs unix.Statfs_t
	if err := unix.Fstatfs(fd, &fs); err != nil || uint64(fs.Type) != fenceExt4Magic {
		return fmt.Errorf("the data folder's filesystem (type %#x) is not ext4", uint64(fs.Type))
	}
	mount, err := fenceMountID(fd)
	if err != nil {
		return err
	}
	if kind, err := fenceMountFSType(mount); err != nil || kind != "ext4" {
		return fmt.Errorf("the data folder's filesystem is %q, not ext4", kind)
	}
	return nil
}
