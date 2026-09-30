//go:build linux

package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"golang.org/x/sys/unix"
)

// fenceControl is the control folder K: the switch's locks, journal, reports, the retained trees
// and the trash they pass through on removal. It sits beside data, never inside it, so nothing the
// updater holds open is ever part of a fenced tree.
type fenceControl struct {
	file  *os.File
	path  string
	key   [2]uint64
	sub   map[string]*os.File
	locks []*os.File
	seams *fenceSeams
}

var fenceControlSubdirs = []string{"locks", "journal", "reports", "retained", "trash"}

// fenceOpenControl opens (creating as needed) the control folder at name, a path relative to the
// data folder's parent: .daedalus-update/<data folder's name> unless a test says otherwise. One
// folder per data folder, because two data folders may share a parent (--data), and one's update
// must never see, let alone remove, the other's kept copies or settle its unfinished switch.
func fenceOpenControl(parent *os.File, name string, euid uint32, dev uint64, seams *fenceSeams) (*fenceControl, string) {
	dir, fd := parent, -1
	for i, part := range strings.Split(filepath.Clean(name), string(filepath.Separator)) {
		next, reason := fenceMkdirPrivate(dir, part, euid, seams)
		if i > 0 {
			dir.Close()
		}
		if reason != "" {
			return nil, "the control folder: " + reason
		}
		fd = next
		dir = os.NewFile(uintptr(fd), part)
	}
	var st unix.Stat_t
	if err := unix.Fstat(fd, &st); err != nil || uint64(st.Dev) != dev {
		dir.Close()
		return nil, "the control folder is not on the data folder's filesystem"
	}
	k := &fenceControl{file: dir, path: filepath.Join(parent.Name(), name),
		key: [2]uint64{uint64(st.Dev), st.Ino}, sub: make(map[string]*os.File), seams: seams}
	for _, sub := range fenceControlSubdirs {
		subFD, reason := fenceMkdirPrivate(k.file, sub, euid, seams)
		if reason != "" {
			k.close()
			return nil, "the control folder: " + reason
		}
		k.sub[sub] = os.NewFile(uintptr(subFD), sub)
	}
	return k, ""
}

func (k *fenceControl) close() {
	for _, f := range k.sub {
		f.Close()
	}
	k.file.Close()
}

// lock takes the control folder's own locks. They exclude a second switch, and a removal running
// beside a switch, on the same data folder.
func (k *fenceControl) lock() string {
	for _, name := range []string{"update.lock", "finish.lock"} {
		fd, err := unix.Openat(int(k.sub["locks"].Fd()), name, unix.O_RDWR|unix.O_CREAT|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0o600)
		if err != nil {
			k.unlock()
			return "the switch lock cannot be opened: " + err.Error()
		}
		if err := unix.Flock(fd, unix.LOCK_EX|unix.LOCK_NB); err != nil {
			unix.Close(fd)
			k.unlock()
			return "another switch or removal of this data folder is running"
		}
		k.locks = append(k.locks, os.NewFile(uintptr(fd), name))
	}
	return ""
}

func (k *fenceControl) unlock() {
	for _, f := range k.locks {
		_ = unix.Flock(int(f.Fd()), unix.LOCK_UN)
		f.Close()
	}
	k.locks = nil
}

// writeFile replaces sub/name durably: a temporary file, fsync, rename, fsync of the folder. A write
// that fails removes its temporary file: on a full disk an empty .tmp is one more thing nobody can
// tell apart from a record.
func (k *fenceControl) writeFile(sub, name string, body []byte) (err error) {
	dir := k.sub[sub]
	tmp := name + ".tmp"
	defer func() {
		if err != nil {
			_ = unix.Unlinkat(int(dir.Fd()), tmp, 0)
		}
	}()
	if k.seams != nil && k.seams.controlWrite != nil {
		if err := k.seams.controlWrite(sub, name); err != nil {
			return err
		}
	}
	fd, err := unix.Openat(int(dir.Fd()), tmp, unix.O_WRONLY|unix.O_CREAT|unix.O_TRUNC|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0o600)
	if err != nil {
		return err
	}
	f := os.NewFile(uintptr(fd), tmp)
	if _, err := f.Write(body); err != nil {
		f.Close()
		return err
	}
	if err := f.Sync(); err != nil {
		f.Close()
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	if err := unix.Renameat(int(dir.Fd()), tmp, int(dir.Fd()), name); err != nil {
		return err
	}
	return dir.Sync()
}

func (k *fenceControl) readFile(sub, name string) ([]byte, error) {
	fd, err := unix.Openat(int(k.sub[sub].Fd()), name, unix.O_RDONLY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	if err != nil {
		return nil, err
	}
	f := os.NewFile(uintptr(fd), name)
	defer f.Close()
	var buf strings.Builder
	chunk := make([]byte, 64*1024)
	for {
		n, err := f.Read(chunk)
		buf.Write(chunk[:n])
		if err != nil {
			break
		}
	}
	return []byte(buf.String()), nil
}

func (k *fenceControl) journal(j fenceJournal) error {
	j.Updated = time.Now().UTC()
	body, err := json.Marshal(j)
	if err != nil {
		return err
	}
	return k.writeFile("journal", j.Op+".json", body)
}

// unfinished names a switch that stopped without reaching its end — a crash, or a third party
// that exchanged our trees. Until an operator has looked, no new switch starts.
func (k *fenceControl) unfinished() string {
	names, err := k.sub["journal"].Readdirnames(-1)
	if _, seekErr := k.sub["journal"].Seek(0, 0); seekErr != nil && err == nil {
		err = seekErr
	}
	if err != nil {
		return "the switch journal cannot be read: " + err.Error()
	}
	sort.Strings(names)
	for _, name := range names {
		if !strings.HasSuffix(name, ".json") {
			continue
		}
		body, err := k.readFile("journal", name)
		if err != nil {
			return "the switch journal cannot be read: " + err.Error()
		}
		var j fenceJournal
		if err := json.Unmarshal(body, &j); err != nil {
			return "the switch journal " + name + " is damaged"
		}
		if !fenceJournalFinished(j.Phase) {
			return fmt.Sprintf("an earlier switch (%s) stopped at %q; `daedalus-desktop update resolve` says what is where and settles it without touching the live data", j.Op, j.Phase)
		}
	}
	return ""
}

// errFenceNotDurable is adopt's answer when the tree is filed and recorded but the folders could not
// be synced: the disk reported an error, and a power cut before its own writeback could undo the move.
var errFenceNotDurable = errors.New("the move could not be written to the disk")

// errFenceNotRecorded is adopt's answer when the record could not be written — a full disk above
// all. The tree has not moved; nothing about it changed.
var errFenceNotRecorded = errors.New("the record of the tree could not be written")

// adopt files one of our trees under retained/<meta.Name>, by inode before and after the rename.
// The record is written first — unless the caller wrote it in advance (recorded) — so that no moment
// exists in which the tree is filed without one: a tree without a record can be neither restored
// nor removed, and after a crash nobody could tell what it held. If the name led to something else
// by the time of the rename, that tree is set aside as foreign and the record goes, and a tree
// without a record is never removed. The rename is made durable before this returns.
func (k *fenceControl) adopt(t *fenceTree, meta fenceRetainedMeta, recorded bool) error {
	retained := k.sub["retained"]
	root := t.root()
	name := meta.Name
	if err := fenceSameNamed(root); err != nil {
		return fmt.Errorf("before the move: %w", err)
	}
	if !recorded {
		if meta.Created.IsZero() {
			meta.Created = time.Now().UTC()
		}
		if err := k.writeMeta(meta); err != nil {
			return fmt.Errorf("%w: %v", errFenceNotRecorded, err)
		}
	}
	from := root.parent
	if err := unix.Renameat2(int(from.Fd()), root.name, int(retained.Fd()), name, unix.RENAME_NOREPLACE); err != nil {
		k.forget(name)
		return err
	}
	t.rebind(retained, name)
	if err := fenceSameNamed(root); err != nil {
		k.forget(name)
		_ = unix.Renameat2(int(retained.Fd()), name, int(retained.Fd()), "foreign-"+name, unix.RENAME_NOREPLACE)
		return fmt.Errorf("after the move: %w", err)
	}
	// Both folders the rename changed, so that a power cut cannot bring the tree back under its old
	// name while its record says it is here.
	if err := k.durable("retain", func() error {
		if err := retained.Sync(); err != nil {
			return err
		}
		return from.Sync()
	}); err != nil {
		return fmt.Errorf("%w: the move of %s: %v", errFenceNotDurable, name, err)
	}
	return nil
}

// durable runs one step that makes something survive a power cut, through the test seam first.
func (k *fenceControl) durable(what string, do func() error) error {
	if k.seams != nil && k.seams.sync != nil {
		if err := k.seams.sync(what); err != nil {
			return err
		}
	}
	return do()
}

func (k *fenceControl) writeMeta(meta fenceRetainedMeta) error {
	body, err := json.MarshalIndent(meta, "", "  ")
	if err != nil {
		return err
	}
	return k.writeFile("retained", meta.Name+".json", body)
}

func (k *fenceControl) readMeta(name string) (fenceRetainedMeta, error) {
	var meta fenceRetainedMeta
	body, err := k.readFile("retained", name+".json")
	if err != nil {
		return meta, err
	}
	err = json.Unmarshal(body, &meta)
	return meta, err
}

// forget drops the record of a retained tree that is no longer retained — a kept copy that a
// restore has made live again. Only the record: the tree is data.
func (k *fenceControl) forget(name string) {
	_ = unix.Unlinkat(int(k.sub["retained"].Fd()), name+".json", 0)
}

func (k *fenceControl) markManual(name string, late []string) {
	meta, err := k.readMeta(name)
	if err != nil {
		return
	}
	meta.GC = "manual"
	meta.LateWritePossible = late
	meta.Reason = "a late write may have reached it"
	_ = k.writeMeta(meta)
}

func (k *fenceControl) setMeta(name string, change func(*fenceRetainedMeta)) {
	meta, err := k.readMeta(name)
	if err != nil {
		return
	}
	change(&meta)
	_ = k.writeMeta(meta)
}

func (k *fenceControl) writeGC(g *fenceGCReport) {
	body, err := json.MarshalIndent(g, "", "  ")
	if err != nil {
		return
	}
	_ = k.writeFile("reports", fmt.Sprintf("gc-%s-%d.json", g.Name, g.At.UnixNano()), body)
}

// fenceGCReason turns a problem found while fencing a retained tree into the reason it is kept.
func fenceGCReason(err error) string {
	var problem *fenceProblem
	if errors.As(err, &problem) {
		if problem.Cause == "open for writing" {
			return "held open: " + problem.Path
		}
		return problem.Error()
	}
	return err.Error()
}

// collect removes one retained tree, and only under a fresh fence: every lease granted, no process
// inside it, the full manifest equal to the one recorded when it was retained. It is moved to the
// trash, swept again, and deleted from the bottom up with the leases still held, each file checked
// right before its unlink. Anything amiss before the first unlink keeps the tree (RETAINED); a doubt
// after unlinks have begun is reported as LOST_POSSIBLE, loudly, because it cannot be undone.
// Age and the number of copies are reasons to try this, never reasons to delete.
func (k *fenceControl) collect(name string, operator bool, euid int, seams *fenceSeams) (g fenceGCReport) {
	g = fenceGCReport{Name: name, Outcome: fenceRetainedGC, At: time.Now().UTC()}
	defer k.writeGC(&g)
	meta, err := k.readMeta(name)
	if err != nil {
		g.Reason = "no record of what this tree held; it is kept: " + err.Error()
		return g
	}
	if meta.Status == fenceLostPossible {
		g.Reason = "an earlier removal may have lost a write; kept for the operator"
		return g
	}
	if meta.GC != "auto" && !operator {
		g.Reason = "kept until the operator removes it: " + meta.Reason
		return g
	}
	if meta.Status == fenceStatusRemoving {
		// A removal that was cut off (a crash, a kill) left the rest of the tree in the trash. It
		// goes on from there, under the same fence, and what is left must still be exactly as
		// recorded.
		k.remove(k.sub["trash"], name, meta, euid, seams, &g)
		return g
	}
	k.remove(k.sub["retained"], name, meta, euid, seams, &g)
	return g
}

// remove is collect's work for a tree at dir/name whose record is meta — a tree under retained, or
// the slot of a switch that never exchanged, whose record was never written: on a full disk the
// record may not fit, and the slot is exactly what has to go to free the space. Whatever does not
// end in DELETED leaves the tree under retained with its record, or where it was when the removal
// never touched it.
func (k *fenceControl) remove(dir *os.File, name string, meta fenceRetainedMeta, euid int, seams *fenceSeams, g *fenceGCReport) {
	retained, trash := k.sub["retained"], k.sub["trash"]
	inRetained, resume := dir == retained, dir == trash
	// keep records the tree as kept for the operator, wherever it is left.
	keep := func(change func(*fenceRetainedMeta)) {
		change(&meta)
		_ = k.writeMeta(meta)
	}
	sig := fenceSIGIO()
	abort := func() error {
		if fenceSIGIO() != sig {
			return errFenceSignal
		}
		return nil
	}
	leases := 0
	r, err := fenceOpenTree("R", dir, name, euid, seams, &leases)
	if err != nil {
		g.Reason = fenceGCReason(err)
		return
	}
	defer r.close()
	if seams != nil && seams.gcAfterFence != nil {
		seams.gcAfterFence(r)
	}
	current, err := r.manifest(abort)
	if err != nil {
		g.Reason = fenceGCReason(err)
		return
	}
	refs, unscanned, limits := fenceScanProcs(r.inodes())
	g.Unscanned, g.Limits = unscanned, limits
	if len(refs) > 0 {
		for _, ref := range refs {
			g.Entries = append(g.Entries, fenceEntryReport{Tree: "R", Path: ref.Path, Cause: "a process is inside it (" + ref.What + ")", PID: ref.PID, TID: ref.TID})
		}
		g.Reason = "held open: a process is inside it"
		return
	}
	var diffs []string
	for _, diff := range fenceManifestDiff(meta.Manifest, current) {
		// What the removal that was cut off already unlinked is missing, and only that may be; a
		// directory it emptied has fewer links and an mtime of its own doing.
		if resume && (strings.HasPrefix(diff, "missing: ") || meta.Manifest[strings.TrimPrefix(diff, "changed: ")].Kind == "dir") {
			continue
		}
		diffs = append(diffs, diff)
	}
	if len(diffs) > 0 {
		for _, diff := range diffs {
			g.Entries = append(g.Entries, fenceEntryReport{Tree: "R", Path: diff, Cause: "differs from the record"})
		}
		g.Reason = "changed after it was recorded: possible late write"
		return
	}
	if err := fenceSameNamed(r.root()); err != nil {
		g.Reason = "not where it was recorded: " + err.Error()
		return
	}
	if resume {
		k.finishRemoval(r, meta, abort, seams, g, keep)
		return
	}
	// The trash is written to before the first unlink: a removal cut off half-way leaves a tree in
	// the trash that update status names, with the record that says what it was.
	meta.Status = fenceStatusRemoving
	if err := k.writeMeta(meta); err != nil && inRetained {
		g.Reason = "the removal could not be recorded: " + err.Error()
		return
	}
	if err := unix.Renameat2(int(dir.Fd()), name, int(trash.Fd()), meta.Name, unix.RENAME_NOREPLACE); err != nil {
		meta.Status = ""
		_ = k.writeMeta(meta)
		if !inRetained {
			k.forget(meta.Name)
		}
		g.Reason = "cannot be moved to the trash: " + err.Error()
		return
	}
	r.rebind(trash, meta.Name)
	if err := fenceSameNamed(r.root()); err != nil {
		g.Reason = "another tree arrived in the trash in its place: " + err.Error()
		keep(func(m *fenceRetainedMeta) { m.GC = "manual"; m.Reason = g.Reason })
		return
	}
	if seams != nil && seams.gcAfterTrash != nil {
		seams.gcAfterTrash(r)
	}
	ledger := &fenceLedger{rootMoves: 1}
	sweep := r.sweep(ledger)
	if abort() != nil {
		sweep.add("R", ".", "a lease-break signal arrived")
	}
	if ledger.rootMoves > 0 {
		sweep.add("R", ".", "the move's own event is missing")
	}
	if fenceSweepFails(sweep) {
		g.Entries, g.Overflow = sweep.Entries, sweep.Overflow
		g.Reason = "activity during the removal fence"
		if err := k.putBack(r, meta.Name); err != nil {
			g.Reason += "; it stays in the trash: " + err.Error()
			keep(func(m *fenceRetainedMeta) { m.GC = "manual"; m.Reason = g.Reason })
			return
		}
		keep(func(m *fenceRetainedMeta) { m.Status = "" })
		return
	}
	k.finishRemoval(r, meta, abort, seams, g, keep)
}

// finishRemoval is the unlinking, from the trash, and the record's end.
func (k *fenceControl) finishRemoval(r *fenceTree, meta fenceRetainedMeta, abort func() error, seams *fenceSeams, g *fenceGCReport, keep func(func(*fenceRetainedMeta))) {
	k.delete(r, meta.Name, abort, seams, g)
	switch g.Outcome {
	case fenceDeleted:
		k.forget(meta.Name)
	case fenceLostPossible:
		keep(func(m *fenceRetainedMeta) { m.GC = "manual"; m.Status = fenceLostPossible; m.Reason = g.Reason })
	default:
		if err := k.putBack(r, meta.Name); err != nil {
			g.Reason += "; it stays in the trash: " + err.Error()
			keep(func(m *fenceRetainedMeta) { m.GC = "manual"; m.Reason = "partly removed: " + g.Reason })
			return
		}
		keep(func(m *fenceRetainedMeta) {
			m.GC = "manual"
			m.Status = ""
			m.Reason = "partly removed: " + g.Reason
		})
	}
}

// putBack returns a tree from the trash to retained, by inode.
func (k *fenceControl) putBack(r *fenceTree, name string) error {
	if err := fenceSameNamed(r.root()); err != nil {
		return err
	}
	if err := unix.Renameat2(int(k.sub["trash"].Fd()), name, int(k.sub["retained"].Fd()), name, unix.RENAME_NOREPLACE); err != nil {
		return err
	}
	r.rebind(k.sub["retained"], name)
	return fenceSameNamed(r.root())
}

// delete is step six: the unlinks, deepest directory first, with the leases still held. Before
// every file's unlink its lease and size are checked; every so often, and before every rmdir, the
// event queue is read and the leases of files already unlinked are checked again. Our own IN_DELETE,
// DELETE_SELF and IGNORED are expected; everything else is someone else's.
func (k *fenceControl) delete(r *fenceTree, name string, abort func() error, seams *fenceSeams, g *fenceGCReport) {
	ledger := &fenceLedger{deletes: make(map[string]int), gone: make(map[string]bool)}
	children := make(map[string][]*fenceNode)
	for _, group := range [][]*fenceNode{r.files, r.links} {
		for _, node := range group {
			dir := path.Dir(node.rel)
			children[dir] = append(children[dir], node)
		}
	}
	lostOnGone := func() []string {
		var broken []string
		for _, node := range r.files {
			if !node.gone {
				continue
			}
			if lease, err := unix.FcntlInt(node.file.Fd(), unix.F_GETLEASE, 0); err != nil || lease != unix.F_RDLCK {
				broken = append(broken, node.rel)
			}
		}
		return broken
	}
	goneRel := func(rel string) bool {
		for _, group := range [][]*fenceNode{r.dirs, r.files, r.links} {
			for _, node := range group {
				if node.rel == rel && node.gone {
					return true
				}
			}
		}
		return false
	}
	stop := func(lost bool, reason string, entries []fenceEntryReport) {
		g.Entries = append(g.Entries, entries...)
		g.Reason = reason
		if lost {
			g.Outcome = fenceLostPossible
		} else {
			g.Outcome = fenceRetainedGC
		}
	}
	// check reads the queue and the leases of what is already gone. It returns true when removal
	// must stop, having set the outcome.
	check := func() bool {
		var sweep fenceSweepResult
		r.drain(ledger, &sweep)
		if sweep.Overflow {
			g.Overflow = true
		}
		broken := lostOnGone()
		lost := fenceObservationVoid(sweep) || len(broken) > 0
		for _, entry := range sweep.Entries {
			if goneRel(entry.Path) {
				lost = true
			}
		}
		if abort() != nil && !lost {
			sweep.add("R", ".", "a lease-break signal arrived")
		}
		for _, rel := range broken {
			sweep.add("R", rel, "lease broken after the unlink: a writer may have written into the removed file")
		}
		if fenceSweepFails(sweep) || lost {
			reason := "activity during the removal"
			if lost {
				reason = "a write may have been lost during the removal"
			}
			stop(lost, reason, sweep.Entries)
			return true
		}
		return false
	}
	unlinked := 0
	// Names of one file that this removal already took: a file with two names in the tree has one
	// link fewer after the first unlink, and that is ours, not someone else's.
	taken := make(map[[2]uint64]uint64)
	for i := len(r.dirs) - 1; i >= 0; i-- {
		dir := r.dirs[i]
		for _, node := range children[dir.rel] {
			if node.kind == "file" {
				var st unix.Stat_t
				lease, err := unix.FcntlInt(node.file.Fd(), unix.F_GETLEASE, 0)
				if err != nil || lease != unix.F_RDLCK || unix.Fstat(int(node.file.Fd()), &st) != nil || st.Size != node.entry.Size || uint64(st.Nlink) != node.entry.Nlink-taken[[2]uint64{node.dev, node.ino}] {
					if broken := lostOnGone(); len(broken) > 0 {
						stop(true, "a write may have been lost during the removal", []fenceEntryReport{{Tree: "R", Path: strings.Join(broken, ", "), Cause: "lease broken after the unlink"}})
						return
					}
					stop(false, "a writer reached a file before its removal", []fenceEntryReport{{Tree: "R", Path: node.rel, Cause: "lease broken or size changed"}})
					return
				}
			}
			if seams != nil && seams.gcBeforeUnlink != nil {
				seams.gcBeforeUnlink(r, node.rel)
			}
			if err := unix.Unlinkat(int(dir.file.Fd()), node.name, 0); err != nil {
				stop(len(lostOnGone()) > 0, "an unlink failed: "+err.Error(), []fenceEntryReport{{Tree: "R", Path: node.rel, Cause: "unlink failed"}})
				return
			}
			ledger.deletes[dir.rel+"\x00"+node.name]++
			taken[[2]uint64{node.dev, node.ino}]++
			node.gone = true
			unlinked++
			g.Deleted = unlinked
			if unlinked%1000 == 0 && check() {
				return
			}
		}
		if check() {
			return
		}
		var err error
		if i == 0 {
			err = unix.Unlinkat(int(k.sub["trash"].Fd()), name, unix.AT_REMOVEDIR)
		} else {
			err = unix.Unlinkat(int(dir.parent.Fd()), dir.name, unix.AT_REMOVEDIR)
			ledger.deletes[path.Dir(dir.rel)+"\x00"+dir.name]++
		}
		if err != nil {
			stop(len(lostOnGone()) > 0, "a directory could not be removed (something new is in it?): "+err.Error(), []fenceEntryReport{{Tree: "R", Path: dir.rel, Cause: "rmdir failed"}})
			return
		}
		ledger.gone[dir.rel] = true
		dir.gone = true
		unlinked++
		g.Deleted = unlinked
	}
	if seams != nil && seams.gcAfterDelete != nil {
		seams.gcAfterDelete(r)
	}
	// Step seven: after the last unlink, every lease once more and the rest of the queue.
	if check() {
		if g.Outcome != fenceLostPossible {
			// Everything is already unlinked; any doubt now is about data that is gone.
			g.Outcome = fenceLostPossible
			g.Reason = "a write may have been lost during the removal"
		}
		return
	}
	g.Outcome = fenceDeleted
	g.Reason = ""
}

// fenceCollectRetained is the operator's or the launcher's entry point for removing one retained
// tree. It refuses where the fence cannot be trusted exactly as the switch does.
func fenceCollectRetained(data, control, name string, operator bool, seams *fenceSeams) fenceGCReport {
	g := fenceGCReport{Name: name, Outcome: fenceRetainedGC, At: time.Now().UTC()}
	fenceSIGIO()
	s := &fenceSwitch{seams: seams}
	euid := s.euidNow()
	if seams == nil {
		seams = &fenceSeams{}
	}
	switch {
	case euid == 0:
		g.Reason = "running as root: no removal"
	case s.seamBool(seams.container, fenceInContainer):
		g.Reason = "running inside a container: no removal"
	case s.seamBool(seams.capLease, fenceHasCapLease):
		g.Reason = "this process holds CAP_LEASE: no removal"
	case !s.seamBool(seams.leasesEnabled, fenceLeasesEnabled):
		g.Reason = "file leases are disabled: no removal"
	}
	if g.Reason != "" {
		return g
	}
	if operator {
		// The kept copy an unresolved update would roll back to is not the operator's to remove from
		// the page: that update's rollback is the only thing still counting on it.
		if paths, err := NewPaths(data); err == nil {
			if journal, err := readJournal(paths); err == nil && journal.unresolved() && journal.Pre == name {
				g.Reason = "an update that did not finish still needs it to roll back; run `upgrade --rollback` first"
				return g
			}
		}
	}
	if control == "" {
		control = fenceControlName(data)
	}
	parentPath := filepath.Dir(filepath.Clean(data))
	pfd, err := unix.Open(parentPath, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_CLOEXEC, 0)
	if err != nil {
		g.Reason = "the data folder's parent cannot be opened: " + err.Error()
		return g
	}
	parent := os.NewFile(uintptr(pfd), parentPath)
	defer parent.Close()
	var st unix.Stat_t
	if err := unix.Fstat(pfd, &st); err != nil {
		g.Reason = err.Error()
		return g
	}
	k, reason := fenceOpenControl(parent, control, uint32(euid), uint64(st.Dev), seams)
	if reason != "" {
		g.Reason = reason
		return g
	}
	defer k.close()
	if busy := k.lock(); busy != "" {
		g.Reason = busy
		return g
	}
	defer k.unlock()
	return k.collect(name, operator, euid, seams)
}
