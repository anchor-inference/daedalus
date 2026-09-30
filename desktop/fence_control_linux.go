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
}

var fenceControlSubdirs = []string{"locks", "journal", "reports", "retained", "trash"}

func fenceOpenControl(parent *os.File, name string, euid uint32, dev uint64, seams *fenceSeams) (*fenceControl, string) {
	fd, reason := fenceMkdirPrivate(parent, name, euid, seams)
	if reason != "" {
		return nil, "the control folder: " + reason
	}
	var st unix.Stat_t
	if err := unix.Fstat(fd, &st); err != nil || uint64(st.Dev) != dev {
		unix.Close(fd)
		return nil, "the control folder is not on the data folder's filesystem"
	}
	k := &fenceControl{file: os.NewFile(uintptr(fd), name), path: filepath.Join(parent.Name(), name),
		key: [2]uint64{uint64(st.Dev), st.Ino}, sub: make(map[string]*os.File)}
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

// writeFile replaces sub/name durably: a temporary file, fsync, rename, fsync of the folder.
func (k *fenceControl) writeFile(sub, name string, body []byte) error {
	dir := k.sub[sub]
	tmp := name + ".tmp"
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

func (k *fenceControl) journal(j fenceJournal) {
	j.Updated = time.Now().UTC()
	body, _ := json.Marshal(j)
	_ = k.writeFile("journal", j.Op+".json", body)
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

// adopt files one of our trees under retained/<name>, by inode before and after the rename. If the
// name led to something else by the time of the rename, that tree is set aside as foreign with no
// record, and a tree without a record is never removed.
func (k *fenceControl) adopt(t *fenceTree, name, op, kind string, manifest fenceManifest, gc, reason string) error {
	retained := k.sub["retained"]
	root := t.root()
	if err := fenceSameNamed(root); err != nil {
		return fmt.Errorf("before the move: %w", err)
	}
	if err := unix.Renameat2(int(root.parent.Fd()), root.name, int(retained.Fd()), name, unix.RENAME_NOREPLACE); err != nil {
		return err
	}
	t.rebind(retained, name)
	if err := fenceSameNamed(root); err != nil {
		_ = unix.Renameat2(int(retained.Fd()), name, int(retained.Fd()), "foreign-"+name, unix.RENAME_NOREPLACE)
		return fmt.Errorf("after the move: %w", err)
	}
	meta := fenceRetainedMeta{Name: name, Kind: kind, Op: op, GC: gc, Reason: reason, Created: time.Now().UTC(), Manifest: manifest}
	return k.writeMeta(meta)
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
	retained, trash := k.sub["retained"], k.sub["trash"]
	sig := fenceSIGIO()
	abort := func() error {
		if fenceSIGIO() != sig {
			return errFenceSignal
		}
		return nil
	}
	leases := 0
	r, err := fenceOpenTree("R", retained, name, euid, seams, &leases)
	if err != nil {
		g.Reason = fenceGCReason(err)
		return g
	}
	defer r.close()
	if seams != nil && seams.gcAfterFence != nil {
		seams.gcAfterFence(r)
	}
	current, err := r.manifest(abort)
	if err != nil {
		g.Reason = fenceGCReason(err)
		return g
	}
	refs, unscanned, limits := fenceScanProcs(r.inodes())
	g.Unscanned, g.Limits = unscanned, limits
	if len(refs) > 0 {
		for _, ref := range refs {
			g.Entries = append(g.Entries, fenceEntryReport{Tree: "R", Path: ref.Path, Cause: "a process is inside it (" + ref.What + ")", PID: ref.PID, TID: ref.TID})
		}
		g.Reason = "held open: a process is inside it"
		return g
	}
	if diffs := fenceManifestDiff(meta.Manifest, current); len(diffs) > 0 {
		for _, diff := range diffs {
			g.Entries = append(g.Entries, fenceEntryReport{Tree: "R", Path: diff, Cause: "differs from the record"})
		}
		g.Reason = "changed after it was recorded: possible late write"
		return g
	}
	if err := fenceSameNamed(r.root()); err != nil {
		g.Reason = "not where it was recorded: " + err.Error()
		return g
	}
	if err := unix.Renameat2(int(retained.Fd()), name, int(trash.Fd()), name, unix.RENAME_NOREPLACE); err != nil {
		g.Reason = "cannot be moved to the trash: " + err.Error()
		return g
	}
	r.rebind(trash, name)
	if err := fenceSameNamed(r.root()); err != nil {
		g.Reason = "another tree arrived in the trash in its place: " + err.Error()
		k.setMeta(name, func(m *fenceRetainedMeta) { m.GC = "manual"; m.Reason = g.Reason })
		return g
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
		if err := k.putBack(r, name); err != nil {
			g.Reason += "; it stays in the trash: " + err.Error()
			k.setMeta(name, func(m *fenceRetainedMeta) { m.GC = "manual"; m.Reason = g.Reason })
		}
		return g
	}
	k.delete(r, name, abort, seams, &g)
	switch g.Outcome {
	case fenceDeleted:
		_ = unix.Unlinkat(int(retained.Fd()), name+".json", 0)
	case fenceLostPossible:
		k.setMeta(name, func(m *fenceRetainedMeta) { m.GC = "manual"; m.Status = fenceLostPossible; m.Reason = g.Reason })
	default:
		if err := k.putBack(r, name); err != nil {
			g.Reason += "; it stays in the trash: " + err.Error()
		}
		k.setMeta(name, func(m *fenceRetainedMeta) {
			m.GC = "manual"
			m.Reason = "partly removed: " + g.Reason
		})
	}
	return g
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
	if control == "" {
		control = ".daedalus-update"
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
