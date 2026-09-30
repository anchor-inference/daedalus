//go:build linux

package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"os/signal"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"

	"golang.org/x/sys/unix"
)

// The inotify mask is exactly this. IN_ACCESS, IN_OPEN and IN_CLOSE_NOWRITE are left out on purpose:
// every reader produces them, and a reader is harmless (a read lease is granted beside it), so they
// would turn each backup program or indexer into a refusal. IN_MODIFY is in because
// open(O_RDONLY|O_TRUNC) empties a file without breaking a read lease; the event, and the size
// check in every sweep, are what see it.
const fenceMask = unix.IN_MODIFY | unix.IN_ATTRIB | unix.IN_CLOSE_WRITE | unix.IN_MOVED_FROM |
	unix.IN_MOVED_TO | unix.IN_CREATE | unix.IN_DELETE | unix.IN_DELETE_SELF | unix.IN_MOVE_SELF |
	unix.IN_ONLYDIR

// EXTENTS, INDEX and INLINE_DATA describe how ext4 stores an inode, not what is in it; a byte-exact
// copy may differ in them. Every other inode flag either changes how data is written (append-only,
// immutable, data journalling) or is not reproduced by a copy, so any of them fails the switch shut.
const fenceServiceFlags = 0x00080000 | 0x00001000 | 0x10000000

// SIGIO is how the kernel tells a lease holder that a writer is waiting. Its default action kills
// the process, so it is caught once for the whole life of the process and never released: a
// signal still in flight after a fence is dropped must not find the default action back in place.
var fenceSignal struct {
	once  sync.Once
	count atomic.Uint64
}

func fenceSIGIO() uint64 {
	fenceSignal.once.Do(func() {
		ch := make(chan os.Signal, 256)
		signal.Notify(ch, syscall.SIGIO)
		go func() {
			for range ch {
				fenceSignal.count.Add(1)
			}
		}()
	})
	return fenceSignal.count.Load()
}

// fenceProblem is a reason the fence cannot be held, with the outcome it forces at the point it was
// found. The caller may harden it (an update refusal becomes a GC retention) but never soften it.
type fenceProblem struct {
	Outcome string
	Tree    string
	Path    string
	Cause   string
	Err     error
}

func (p *fenceProblem) Error() string {
	if p.Err != nil {
		return fmt.Sprintf("%s %s: %s: %v", p.Tree, p.Path, p.Cause, p.Err)
	}
	return fmt.Sprintf("%s %s: %s", p.Tree, p.Path, p.Cause)
}

func (p *fenceProblem) Unwrap() error { return p.Err }

func fenceFail(tree, path, cause string, err error) *fenceProblem {
	return &fenceProblem{Outcome: fenceFailClosed, Tree: tree, Path: path, Cause: cause, Err: err}
}

type fenceEvent struct {
	Dir    string
	Name   string
	Mask   uint32
	Cookie uint32
}

// fenceEventSource is where a tree's inotify events come from. Tests put a wrapper in front of the
// real queue to inject an overflow at an exact phase; the decision about what an overflow means is
// made by the caller, never here.
type fenceEventSource interface {
	read() ([]fenceEvent, error)
}

type fenceInotify struct{ tree *fenceTree }

func (s fenceInotify) read() ([]fenceEvent, error) { return s.tree.readEvents() }

// fenceSeams are the points a test uses to act at an exact moment or to inject a kernel answer that
// cannot be produced without privileges. Production passes none.
type fenceSeams struct {
	euid              func() int
	container         func() bool
	capLease          func() bool
	leasesEnabled     func() bool
	statEntry         func(tree, rel string, st *unix.Stat_t)
	addWatch          func(tree, rel string) error
	fchownGroup       func(rel string) error
	privateMode       func(name string, mode uint32) uint32
	selfProbe         func(*fenceProbeResult)
	afterWatch        func(tree, rel string)
	duringCopy        func(p *fenceTree)
	beforeCFence      func()
	afterCFence       func(c *fenceTree)
	afterCompare      func(p, c *fenceTree)
	afterSweep1       func(p, c *fenceTree)
	afterExchange     func(p, c *fenceTree)
	afterSweep2       func(p, c *fenceTree)
	afterFinalSweep   func(p, c *fenceTree)
	beforeRelease     func(p, c *fenceTree)
	afterLeaseRelease func()
	gcAfterFence      func(r *fenceTree)
	gcAfterTrash      func(r *fenceTree)
	gcBeforeUnlink    func(r *fenceTree, rel string)
	gcAfterDelete     func(r *fenceTree)
}

func (s *fenceSeams) call(f func()) {
	if s != nil && f != nil {
		f()
	}
}

type fenceNode struct {
	rel    string
	kind   string
	file   *os.File // nil for a symlink: it has no descriptor and no lease; its directory's watch covers it
	parent *os.File
	name   string
	dev    uint64
	ino    uint64
	entry  fenceEntry
	gone   bool // removed by our own GC
}

// fenceTree is one tree under the fence: every directory held open and watched, every regular file
// held open with a read lease. The held descriptors are the tree's identity — a path proves nothing
// once someone can rename.
type fenceTree struct {
	label   string
	fd      int
	wds     map[int]string
	source  fenceEventSource
	dirs    []*fenceNode
	files   []*fenceNode
	links   []*fenceNode
	dev     uint64
	mountID uint64
	euid    uint32
	seams   *fenceSeams
	leases  *int
	closed  bool
}

func (t *fenceTree) root() *fenceNode { return t.dirs[0] }

// rebind records the root's new place after one of our own renames.
func (t *fenceTree) rebind(parent *os.File, name string) {
	t.dirs[0].parent = parent
	t.dirs[0].name = name
}

// fenceOpenTree fences the tree at parent/name. On any problem everything taken so far is released
// and the problem says which outcome it forces.
func fenceOpenTree(label string, parent *os.File, name string, euid int, seams *fenceSeams, leases *int) (*fenceTree, error) {
	t := &fenceTree{label: label, fd: -1, wds: make(map[int]string), euid: uint32(euid), seams: seams, leases: leases}
	t.source = fenceInotify{tree: t}
	fd, err := unix.InotifyInit1(unix.IN_NONBLOCK | unix.IN_CLOEXEC)
	if err != nil {
		return nil, fenceFail(label, ".", "inotify cannot be set up", err)
	}
	t.fd = fd
	rootFD, err := unix.Openat(int(parent.Fd()), name, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	if err != nil {
		t.close()
		return nil, fenceFail(label, ".", "the tree cannot be opened as a directory", err)
	}
	root := os.NewFile(uintptr(rootFD), name)
	var st unix.Stat_t
	if err := unix.Fstat(rootFD, &st); err != nil {
		root.Close()
		t.close()
		return nil, fenceFail(label, ".", "stat", err)
	}
	t.dev = uint64(st.Dev)
	if t.mountID, err = fenceMountID(rootFD); err != nil {
		root.Close()
		t.close()
		return nil, fenceFail(label, ".", "mount identity unavailable", err)
	}
	if err := t.walk(root, ".", parent, name); err != nil {
		t.close()
		return nil, err
	}
	return t, nil
}

func (t *fenceTree) stat(rel string, st *unix.Stat_t) {
	if t.seams != nil && t.seams.statEntry != nil {
		t.seams.statEntry(t.label, rel, st)
	}
}

// checkOwned rejects anything not ours or not on this tree's filesystem. A lease on another user's
// file is refused with EACCES, and a mount inside the tree is a filesystem nobody has probed.
func (t *fenceTree) checkOwned(rel string, st *unix.Stat_t) error {
	if st.Uid != t.euid {
		return fenceFail(t.label, rel, fmt.Sprintf("owned by uid %d, not by this user: no fence possible", st.Uid), nil)
	}
	if uint64(st.Dev) != t.dev {
		return fenceFail(t.label, rel, "a mount inside the data folder", nil)
	}
	return fencePermissions(t.label, rel, st)
}

// fencePermissions refuses what the owner itself cannot fully handle. A file its owner cannot read
// cannot be leased or copied, and would refuse every switch with a misleading reason; a directory
// its owner cannot write or search could be copied, but the kept tree could then never be removed.
func fencePermissions(tree, rel string, st *unix.Stat_t) error {
	switch st.Mode & unix.S_IFMT {
	case unix.S_IFDIR:
		if st.Mode&0o700 != 0o700 {
			return fenceFail(tree, rel, fmt.Sprintf("a directory its owner cannot fully use (mode %#o): its kept copy could never be removed", st.Mode&0o7777), nil)
		}
	case unix.S_IFREG:
		if st.Mode&0o400 == 0 {
			return fenceFail(tree, rel, fmt.Sprintf("a file its owner cannot read (mode %#o)", st.Mode&0o7777), nil)
		}
	}
	return nil
}

func fenceJoin(dir, name string) string {
	if dir == "." {
		return name
	}
	return dir + "/" + name
}

// walk fences one directory and, recursively, everything under it. The order is the point: the
// directory is watched before it is listed, so a name created while we list is either in the listing
// or produces an IN_CREATE. A walk that lists first (os.walk, filepath.WalkDir) leaves a window in
// which a new directory is neither listed nor watched, and stays unobserved for good.
func (t *fenceTree) walk(dir *os.File, rel string, parent *os.File, name string) error {
	var st unix.Stat_t
	if err := unix.Fstat(int(dir.Fd()), &st); err != nil {
		dir.Close()
		return fenceFail(t.label, rel, "stat", err)
	}
	t.stat(rel, &st)
	node := &fenceNode{rel: rel, kind: "dir", file: dir, parent: parent, name: name, dev: uint64(st.Dev), ino: st.Ino}
	t.dirs = append(t.dirs, node)
	if err := t.checkOwned(rel, &st); err != nil {
		return err
	}
	if mountID, err := fenceMountID(int(dir.Fd())); err != nil || mountID != t.mountID {
		return fenceFail(t.label, rel, "a mount inside the data folder", err)
	}
	if err := fenceCheckFlags(t.label, rel, int(dir.Fd())); err != nil {
		return err
	}
	if err := t.watch(node); err != nil {
		return err
	}
	if t.seams != nil && t.seams.afterWatch != nil {
		t.seams.afterWatch(t.label, rel)
	}
	names, err := dir.Readdirnames(-1)
	if err != nil {
		return fenceFail(t.label, rel, "the directory cannot be listed", err)
	}
	sort.Strings(names)
	for _, childName := range names {
		childRel := fenceJoin(rel, childName)
		var child unix.Stat_t
		if err := unix.Fstatat(int(dir.Fd()), childName, &child, unix.AT_SYMLINK_NOFOLLOW); err != nil {
			// Gone between listing and stat: someone is changing the tree under the watch. The
			// IN_DELETE is already queued; say it here too.
			return &fenceProblem{Outcome: fenceRefused, Tree: t.label, Path: childRel, Cause: "changed while it was being fenced", Err: err}
		}
		t.stat(childRel, &child)
		if err := t.checkOwned(childRel, &child); err != nil {
			return err
		}
		switch child.Mode & unix.S_IFMT {
		case unix.S_IFDIR:
			fd, err := unix.Openat(int(dir.Fd()), childName, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
			if err != nil {
				return &fenceProblem{Outcome: fenceRefused, Tree: t.label, Path: childRel, Cause: "changed while it was being fenced", Err: err}
			}
			var opened unix.Stat_t
			if err := unix.Fstat(fd, &opened); err != nil || opened.Dev != child.Dev || opened.Ino != child.Ino {
				unix.Close(fd)
				return &fenceProblem{Outcome: fenceRefused, Tree: t.label, Path: childRel, Cause: "replaced while it was being fenced", Err: err}
			}
			if err := t.walk(os.NewFile(uintptr(fd), childRel), childRel, dir, childName); err != nil {
				return err
			}
		case unix.S_IFREG:
			if err := t.fenceFile(dir, childRel, childName, &child); err != nil {
				return err
			}
		case unix.S_IFLNK:
			t.links = append(t.links, &fenceNode{rel: childRel, kind: "symlink", parent: dir, name: childName, dev: uint64(child.Dev), ino: child.Ino})
		default:
			return fenceFail(t.label, childRel, "a special file (device, pipe or socket) cannot be copied or fenced", nil)
		}
	}
	return nil
}

func (t *fenceTree) watch(node *fenceNode) error {
	if t.seams != nil && t.seams.addWatch != nil {
		if err := t.seams.addWatch(t.label, node.rel); err != nil {
			return fenceFail(t.label, node.rel, "the directory cannot be watched", err)
		}
	}
	// The watch goes on the held descriptor through /proc/self/fd, so it lands on the directory we
	// opened even if the name was swapped since. IN_DONT_FOLLOW must not be added: on this path it
	// fails with ENOTDIR, and O_NOFOLLOW at open already refused a symlink.
	wd, err := unix.InotifyAddWatch(t.fd, fmt.Sprintf("/proc/self/fd/%d", node.file.Fd()), fenceMask)
	if err != nil {
		return fenceFail(t.label, node.rel, "the directory cannot be watched", err)
	}
	if _, twice := t.wds[wd]; twice {
		return fenceFail(t.label, node.rel, "a directory is reachable twice", nil)
	}
	t.wds[wd] = node.rel
	return nil
}

func (t *fenceTree) fenceFile(dir *os.File, rel, name string, child *unix.Stat_t) error {
	// A file with a second name — inside this tree or anywhere else on the filesystem — is fenced
	// like any other. The lease belongs to the inode, so a write-open or a writable mapping through
	// any name breaks it; and every sweep compares the inode's own size, link count, metadata, flags
	// and attributes, which a truncation, a chmod, a chattr or a setxattr through any name changes. A
	// name outside the tree gives no event on its watches, and needs none.
	// O_NONBLOCK: if someone else holds a lease on the file, our open fails at once instead of
	// breaking their lease and waiting for them.
	fd, err := unix.Openat(int(dir.Fd()), name, unix.O_RDONLY|unix.O_NOFOLLOW|unix.O_CLOEXEC|unix.O_NONBLOCK, 0)
	if err != nil {
		if errors.Is(err, unix.EWOULDBLOCK) {
			return &fenceProblem{Outcome: fenceRefused, Tree: t.label, Path: rel, Cause: "another process holds a lease on it", Err: err}
		}
		return &fenceProblem{Outcome: fenceRefused, Tree: t.label, Path: rel, Cause: "changed while it was being fenced", Err: err}
	}
	file := os.NewFile(uintptr(fd), rel)
	var opened unix.Stat_t
	if err := unix.Fstat(fd, &opened); err != nil || opened.Dev != child.Dev || opened.Ino != child.Ino {
		file.Close()
		return &fenceProblem{Outcome: fenceRefused, Tree: t.label, Path: rel, Cause: "replaced while it was being fenced", Err: err}
	}
	if err := fenceCheckFlags(t.label, rel, fd); err != nil {
		file.Close()
		return err
	}
	node := &fenceNode{rel: rel, kind: "file", file: file, parent: dir, name: name, dev: uint64(opened.Dev), ino: opened.Ino}
	if _, err := unix.FcntlInt(uintptr(fd), unix.F_SETLEASE, unix.F_RDLCK); err != nil {
		file.Close()
		switch {
		case errors.Is(err, unix.EAGAIN):
			// The kernel's own answer: someone holds it writable, through a descriptor or a shared
			// mapping, even one whose descriptor is long closed.
			return &fenceProblem{Outcome: fenceRefused, Tree: t.label, Path: rel, Cause: "open for writing", Err: err}
		case errors.Is(err, unix.EACCES):
			return fenceFail(t.label, rel, "not ours to fence", err)
		}
		return fenceFail(t.label, rel, "the lease cannot be taken", err)
	}
	if t.leases != nil {
		*t.leases++
	}
	t.files = append(t.files, node)
	return nil
}

func fenceCheckFlags(tree, rel string, fd int) error {
	flags, err := unix.IoctlGetInt(fd, unix.FS_IOC_GETFLAGS)
	if err != nil {
		return fenceFail(tree, rel, "inode flags cannot be read", err)
	}
	if uint32(flags)&^fenceServiceFlags != 0 {
		return fenceFail(tree, rel, fmt.Sprintf("inode flags %#x are set; a copy would not reproduce them", uint32(flags)&^fenceServiceFlags), nil)
	}
	return nil
}

func fenceMountID(fd int) (uint64, error) {
	var st unix.Statx_t
	if err := unix.Statx(fd, "", unix.AT_EMPTY_PATH|unix.AT_STATX_DONT_SYNC, unix.STATX_MNT_ID, &st); err != nil {
		return 0, err
	}
	if st.Mask&unix.STATX_MNT_ID == 0 {
		return 0, errors.New("statx gave no mount id")
	}
	return st.Mnt_id, nil
}

func fenceXattrs(list func([]byte) (int, error), get func(string, []byte) (int, error)) (map[string]string, error) {
	size, err := list(nil)
	if err != nil || size == 0 {
		return nil, err
	}
	buf := make([]byte, size)
	n, err := list(buf)
	if err != nil {
		return nil, err
	}
	attrs := make(map[string]string)
	for _, name := range strings.Split(string(buf[:n]), "\x00") {
		if name == "" {
			continue
		}
		vlen, err := get(name, nil)
		if err != nil {
			return nil, err
		}
		value := make([]byte, vlen)
		if vlen > 0 {
			if _, err := get(name, value); err != nil {
				return nil, err
			}
		}
		sum := sha256.Sum256(value)
		attrs[name] = hex.EncodeToString(sum[:])
	}
	return attrs, nil
}

func fenceFDXattrs(fd int) (map[string]string, error) {
	return fenceXattrs(func(b []byte) (int, error) { return unix.Flistxattr(fd, b) },
		func(name string, b []byte) (int, error) { return unix.Fgetxattr(fd, name, b) })
}

// A symlink has no descriptor to read attributes through; the held directory's /proc path with the
// link's name and the l* calls read the link itself and never its target.
func fenceLinkXattrs(dirfd int, name string) (map[string]string, error) {
	path := fmt.Sprintf("/proc/self/fd/%d/%s", dirfd, name)
	return fenceXattrs(func(b []byte) (int, error) { return unix.Llistxattr(path, b) },
		func(n string, b []byte) (int, error) { return unix.Lgetxattr(path, n, b) })
}

// fenceFDEntry measures one held descriptor. The content hash is read through the same descriptor
// the lease is on: opening the path again could reach a different file.
func fenceFDEntry(file *os.File, kind string, abort func() error) (fenceEntry, error) {
	fd := int(file.Fd())
	var st unix.Stat_t
	if err := unix.Fstat(fd, &st); err != nil {
		return fenceEntry{}, err
	}
	flags, err := unix.IoctlGetInt(fd, unix.FS_IOC_GETFLAGS)
	if err != nil {
		return fenceEntry{}, err
	}
	attrs, err := fenceFDXattrs(fd)
	if err != nil {
		return fenceEntry{}, err
	}
	entry := fenceEntry{Kind: kind, Mode: st.Mode, UID: st.Uid, GID: st.Gid, Nlink: uint64(st.Nlink),
		Xattrs: attrs, Flags: uint32(flags) &^ fenceServiceFlags}
	if kind == "file" {
		entry.Size = st.Size
		entry.MtimeNS = st.Mtim.Nano()
		sum, err := fenceHash(fd, st.Size, abort)
		if err != nil {
			return fenceEntry{}, err
		}
		entry.SHA256 = sum
	}
	// A directory's size and mtime are left out: ext4 never shrinks a directory, and our own copy
	// changes the mtime of every directory it fills.
	return entry, nil
}

func fenceHash(fd int, size int64, abort func() error) (string, error) {
	h := sha256.New()
	buf := make([]byte, 1<<20)
	for off := int64(0); off < size; {
		if abort != nil {
			if err := abort(); err != nil {
				return "", err
			}
		}
		n, err := unix.Pread(fd, buf, off)
		if n > 0 {
			h.Write(buf[:n])
			off += int64(n)
		}
		if err != nil {
			return "", err
		}
		if n == 0 {
			return "", fmt.Errorf("file shorter than its size (%d of %d)", off, size)
		}
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

func (t *fenceTree) linkEntry(node *fenceNode) (fenceEntry, error) {
	var st unix.Stat_t
	if err := unix.Fstatat(int(node.parent.Fd()), node.name, &st, unix.AT_SYMLINK_NOFOLLOW); err != nil {
		return fenceEntry{}, err
	}
	if uint64(st.Dev) != node.dev || st.Ino != node.ino {
		return fenceEntry{}, errors.New("the symlink was replaced")
	}
	buf := make([]byte, unix.PathMax)
	n, err := unix.Readlinkat(int(node.parent.Fd()), node.name, buf)
	if err != nil {
		return fenceEntry{}, err
	}
	attrs, err := fenceLinkXattrs(int(node.parent.Fd()), node.name)
	if err != nil {
		return fenceEntry{}, err
	}
	return fenceEntry{Kind: "symlink", Mode: st.Mode, UID: st.Uid, GID: st.Gid, Size: st.Size, Nlink: uint64(st.Nlink),
		MtimeNS: st.Mtim.Nano(), Target: string(buf[:n]), Xattrs: attrs}, nil
}

// manifest measures the whole tree through its held descriptors and remembers each entry for the
// sweeps that follow.
func (t *fenceTree) manifest(abort func() error) (fenceManifest, error) {
	m := make(fenceManifest)
	for _, node := range t.dirs {
		entry, err := fenceFDEntry(node.file, "dir", abort)
		if err != nil {
			return nil, &fenceProblem{Outcome: fenceRefused, Tree: t.label, Path: node.rel, Cause: "cannot be measured", Err: err}
		}
		node.entry = entry
		m[node.rel] = entry
	}
	for _, node := range t.files {
		entry, err := fenceFDEntry(node.file, "file", abort)
		if err != nil {
			return nil, &fenceProblem{Outcome: fenceRefused, Tree: t.label, Path: node.rel, Cause: "cannot be measured", Err: err}
		}
		node.entry = entry
		m[node.rel] = entry
	}
	for _, node := range t.links {
		entry, err := t.linkEntry(node)
		if err != nil {
			return nil, &fenceProblem{Outcome: fenceRefused, Tree: t.label, Path: node.rel, Cause: "cannot be measured", Err: err}
		}
		node.entry = entry
		m[node.rel] = entry
	}
	return m, nil
}

// inodes is every directory and file of the tree by (dev, ino), for finding processes inside it.
func (t *fenceTree) inodes() map[[2]uint64]string {
	set := make(map[[2]uint64]string)
	for _, group := range [][]*fenceNode{t.dirs, t.files} {
		for _, node := range group {
			set[[2]uint64{node.dev, node.ino}] = node.rel
		}
	}
	return set
}

// fenceLedger is what our own operations are expected to produce, so that only the rest counts as
// someone else's activity.
type fenceLedger struct {
	rootMoves int
	deletes   map[string]int  // dir + "\x00" + name of each name we removed
	gone      map[string]bool // directories we removed: their DELETE_SELF and IGNORED are ours
}

// fenceClassify separates foreign events from our own. An overflow is reported on its own and is not
// a foreign event: what it means (refuse, roll back, keep, or say data may be lost) depends on the
// phase, and the phase is the caller's to judge.
func fenceClassify(events []fenceEvent, ledger *fenceLedger) (foreign []fenceEvent, overflow bool) {
	for _, event := range events {
		switch {
		case event.Mask&unix.IN_Q_OVERFLOW != 0:
			overflow = true
		case ledger != nil && ledger.rootMoves > 0 && event.Dir == "." && event.Name == "" && event.Mask == unix.IN_MOVE_SELF:
			ledger.rootMoves--
		case ledger != nil && event.Mask&unix.IN_DELETE != 0 && ledger.deletes[event.Dir+"\x00"+event.Name] > 0:
			ledger.deletes[event.Dir+"\x00"+event.Name]--
		case ledger != nil && event.Mask&(unix.IN_DELETE_SELF|unix.IN_IGNORED) != 0 && event.Mask&^(unix.IN_DELETE_SELF|unix.IN_IGNORED) == 0 && ledger.gone[event.Dir]:
		default:
			foreign = append(foreign, event)
		}
	}
	return foreign, overflow
}

var fenceMaskNames = []struct {
	bit  uint32
	name string
}{
	{unix.IN_MODIFY, "MODIFY"}, {unix.IN_ATTRIB, "ATTRIB"}, {unix.IN_CLOSE_WRITE, "CLOSE_WRITE"},
	{unix.IN_MOVED_FROM, "MOVED_FROM"}, {unix.IN_MOVED_TO, "MOVED_TO"}, {unix.IN_CREATE, "CREATE"},
	{unix.IN_DELETE, "DELETE"}, {unix.IN_DELETE_SELF, "DELETE_SELF"}, {unix.IN_MOVE_SELF, "MOVE_SELF"},
	{unix.IN_UNMOUNT, "UNMOUNT"}, {unix.IN_Q_OVERFLOW, "Q_OVERFLOW"}, {unix.IN_IGNORED, "IGNORED"},
	{unix.IN_ISDIR, "ISDIR"},
}

func fenceMaskString(mask uint32) string {
	var parts []string
	for _, m := range fenceMaskNames {
		if mask&m.bit != 0 {
			parts = append(parts, m.name)
		}
	}
	if len(parts) == 0 {
		return fmt.Sprintf("%#x", mask)
	}
	return strings.Join(parts, "|")
}

func (t *fenceTree) readEvents() ([]fenceEvent, error) {
	var all []fenceEvent
	buf := make([]byte, 64*1024)
	for {
		n, err := unix.Read(t.fd, buf)
		if errors.Is(err, unix.EAGAIN) {
			return all, nil
		}
		if err != nil {
			return all, err
		}
		for pos := 0; pos < n; {
			if pos+unix.SizeofInotifyEvent > n {
				return all, errors.New("short inotify event")
			}
			wd := int(int32(binary.NativeEndian.Uint32(buf[pos:])))
			mask := binary.NativeEndian.Uint32(buf[pos+4:])
			cookie := binary.NativeEndian.Uint32(buf[pos+8:])
			length := int(binary.NativeEndian.Uint32(buf[pos+12:]))
			if pos+unix.SizeofInotifyEvent+length > n {
				return all, errors.New("short inotify event name")
			}
			name := string(bytes.TrimRight(buf[pos+unix.SizeofInotifyEvent:pos+unix.SizeofInotifyEvent+length], "\x00"))
			all = append(all, fenceEvent{Dir: t.wds[wd], Name: name, Mask: mask, Cookie: cookie})
			pos += unix.SizeofInotifyEvent + length
		}
	}
}

// fenceSweepResult is one look at the fence. Entries are foreign activity or broken guarantees;
// the overflow bit is separate on purpose (see fenceClassify).
type fenceSweepResult struct {
	Entries  []fenceEntryReport
	Overflow bool
}

func (r *fenceSweepResult) add(tree, path, cause string) {
	r.Entries = append(r.Entries, fenceEntryReport{Tree: tree, Path: path, Cause: cause})
}

func (r *fenceSweepResult) merge(other fenceSweepResult) {
	r.Entries = append(r.Entries, other.Entries...)
	r.Overflow = r.Overflow || other.Overflow
}

// fenceObservationVoid is the one decision about an overflow: the queue lost events, so what was
// observed proves nothing, whatever else it shows. Each phase turns that into its own outcome.
func fenceObservationVoid(s fenceSweepResult) bool {
	return s.Overflow
}

// fenceSweepFails is the single place where a sweep becomes a verdict.
func fenceSweepFails(s fenceSweepResult) bool {
	return len(s.Entries) > 0 || fenceObservationVoid(s)
}

func (t *fenceTree) drain(ledger *fenceLedger, result *fenceSweepResult) {
	events, err := t.source.read()
	if err != nil {
		result.add(t.label, ".", "inotify cannot be read: "+err.Error())
	}
	foreign, overflow := fenceClassify(events, ledger)
	result.Overflow = result.Overflow || overflow
	for _, event := range foreign {
		path := event.Dir
		if event.Name != "" {
			path = fenceJoin(event.Dir, event.Name)
		}
		result.add(t.label, path, fenceMaskString(event.Mask))
	}
}

func fenceSameNamed(node *fenceNode) error {
	var named unix.Stat_t
	if err := unix.Fstatat(int(node.parent.Fd()), node.name, &named, unix.AT_SYMLINK_NOFOLLOW); err != nil {
		return err
	}
	if uint64(named.Dev) != node.dev || named.Ino != node.ino {
		return errors.New("another inode is at this name")
	}
	return nil
}

// sweep checks the fence at this instant: no foreign event and no overflow, every lease still held,
// every file's size and link count as recorded (open(O_RDONLY|O_TRUNC) and a new hard link break no
// lease), every inode's flags as recorded (chattr through a read-only descriptor gives no event),
// and every name still leading to the inode we hold — a root renamed away and back leaves a single
// coalesced MOVE_SELF, so counting events alone would miss it.
func (t *fenceTree) sweep(ledger *fenceLedger) fenceSweepResult {
	var result fenceSweepResult
	t.drain(ledger, &result)
	for _, node := range t.dirs {
		if node.gone {
			continue
		}
		if err := fenceSameNamed(node); err != nil {
			result.add(t.label, node.rel, "renamed or replaced: "+err.Error())
		}
		var st unix.Stat_t
		if err := unix.Fstat(int(node.file.Fd()), &st); err != nil || st.Mode != node.entry.Mode || st.Uid != node.entry.UID || st.Gid != node.entry.GID {
			result.add(t.label, node.rel, "metadata changed")
		}
		if flags, err := unix.IoctlGetInt(int(node.file.Fd()), unix.FS_IOC_GETFLAGS); err != nil || uint32(flags)&^fenceServiceFlags != node.entry.Flags {
			result.add(t.label, node.rel, "inode flags changed")
		}
	}
	for _, node := range t.files {
		if node.gone {
			continue
		}
		if err := fenceSameNamed(node); err != nil {
			result.add(t.label, node.rel, "renamed or replaced: "+err.Error())
		}
		if lease, err := unix.FcntlInt(node.file.Fd(), unix.F_GETLEASE, 0); err != nil || lease != unix.F_RDLCK {
			result.add(t.label, node.rel, "lease broken: a writer is waiting")
		}
		var st unix.Stat_t
		if err := unix.Fstat(int(node.file.Fd()), &st); err != nil || st.Size != node.entry.Size || uint64(st.Nlink) != node.entry.Nlink ||
			st.Mode != node.entry.Mode || st.Uid != node.entry.UID || st.Gid != node.entry.GID || st.Mtim.Nano() != node.entry.MtimeNS {
			result.add(t.label, node.rel, "size, link count or metadata changed")
		}
		if flags, err := unix.IoctlGetInt(int(node.file.Fd()), unix.FS_IOC_GETFLAGS); err != nil || uint32(flags)&^fenceServiceFlags != node.entry.Flags {
			result.add(t.label, node.rel, "inode flags changed")
		}
		// An attribute set through a name outside the tree gives no event on any watch here.
		if attrs, err := fenceFDXattrs(int(node.file.Fd())); err != nil || !fenceSameXattrs(attrs, node.entry.Xattrs) {
			result.add(t.label, node.rel, "extended attributes changed")
		}
	}
	for _, node := range t.links {
		if node.gone {
			continue
		}
		if err := fenceSameNamed(node); err != nil {
			result.add(t.label, node.rel, "renamed or replaced: "+err.Error())
		}
	}
	// Events that arrived while we looked.
	t.drain(ledger, &result)
	return result
}

func fenceSameXattrs(a, b map[string]string) bool {
	if len(a) != len(b) {
		return false
	}
	for name, value := range a {
		if b[name] != value {
			return false
		}
	}
	return true
}

// brokenLeases names every file whose lease is no longer held.
func (t *fenceTree) brokenLeases() []string {
	var broken []string
	for _, node := range t.files {
		if lease, err := unix.FcntlInt(node.file.Fd(), unix.F_GETLEASE, 0); err != nil || lease != unix.F_RDLCK {
			broken = append(broken, node.rel)
		}
	}
	return broken
}

// releaseChecked drops the fence and reports what reached the tree on the way out: a lease that was
// broken before we let go, a file whose size or link count moved, an event we did not cause.
// Everything is read before any lease is dropped; a writer who comes after that is no longer
// waiting on us.
func (t *fenceTree) releaseChecked(ledger *fenceLedger) (late []string, overflow bool) {
	seen := make(map[string]bool)
	for _, node := range t.files {
		lease, err := unix.FcntlInt(node.file.Fd(), unix.F_GETLEASE, 0)
		var st unix.Stat_t
		statErr := unix.Fstat(int(node.file.Fd()), &st)
		if err != nil || lease != unix.F_RDLCK || statErr != nil || st.Size != node.entry.Size || uint64(st.Nlink) != node.entry.Nlink {
			seen[node.rel] = true
		}
	}
	var result fenceSweepResult
	t.drain(ledger, &result)
	for _, entry := range result.Entries {
		seen[entry.Path] = true
	}
	for path := range seen {
		late = append(late, path)
	}
	sort.Strings(late)
	t.close()
	return late, result.Overflow
}

func (t *fenceTree) close() {
	if t == nil || t.closed {
		return
	}
	t.closed = true
	for _, node := range t.files {
		_, _ = unix.FcntlInt(node.file.Fd(), unix.F_SETLEASE, unix.F_UNLCK)
		_ = node.file.Close()
	}
	for _, node := range t.dirs {
		_ = node.file.Close()
	}
	if t.fd >= 0 {
		_ = unix.Close(t.fd)
	}
}
