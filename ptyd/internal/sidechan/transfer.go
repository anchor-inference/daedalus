package sidechan

import (
	"archive/tar"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"hash"
	"io"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/ascorblack/daedalus/ptyd/internal/config"
)

// Transfers move a whole folder between this machine and the host's other side — a project folder
// carried from the agent's container to the operator's machine, or back — as one tar stream cut
// into calls the size of a frame.
//
// They are the side channels' only writes outside an inbox, so each is held narrower than a read:
//
//   - fs.unpack_* writes only into one new directory: absolute, inside the home directory and not
//     the home directory itself, held to every rule a project folder is (not denied, not the
//     daemon's own), and either missing or empty when the transfer begins. Every entry of the stream
//     is a relative name without `..`; a symbolic link may point only inside the directory, and no
//     entry may be written through a link the stream made. The stream may not carry more than it
//     declared at the start, and an aborted, failed or abandoned transfer removes what it wrote.
//   - fs.pack_* reads one folder under the roots, as a read would, and leaves out (and reports)
//     what a read never opens: files on the deny list, the daemon's own directories, links that point
//     out of the folder, sockets and devices. Its one write is fs.pack_remove, which deletes the
//     folder it has just sent and nothing else, and only when the whole of it went out, nothing was
//     left out, and the folder is still exactly what was sent.
//
// A transfer nobody has called for TransferIdle is abandoned and cleaned up as an abort would: the
// host that started it may have restarted and forgotten it. A transfer also follows the connection it
// is driven over (Own): when that connection closes and no other one takes the transfer up within
// TransferOrphanGrace, it is abandoned then. The idle time alone once kept a host's half-written copy
// for ten minutes after the host restarted, and an operator who tried the move again at once was told
// the folder was not empty — by the very copy that was waiting to be removed.
//
// A finished unpack is kept, not forgotten, until the host releases it (fs.unpack_release) or the idle
// time passes: between the finish and the host's switch to the copy, the host may still decide the
// copy is not wanted, and fs.unpack_abort is then the one way it has to remove a folder it made. A kept
// one is never removed on its own — by then the host may be working in it — and holds no slot.
type Transfers struct {
	fs    *FS
	mu    sync.Mutex
	items map[string]transferItem
	// Idle is how long a transfer may wait for its next call, and Grace how long one whose connection
	// closed waits for another to take it up; tests shorten both.
	Idle  time.Duration
	Grace time.Duration
	// Abandoned, when set, hears of a transfer given up for want of calls, for the daemon's log.
	Abandoned func(id string, why error)
}

type transferItem interface {
	stop(reason error)
	touch()
	adopt(conn context.Context)
	holdsSlot() bool
}

// NewTransfers returns the transfers over a reader's rules.
func NewTransfers(f *FS) *Transfers {
	return &Transfers{fs: f, items: map[string]transferItem{}, Idle: config.TransferIdle, Grace: config.TransferOrphanGrace}
}

// orphanWait is how long a transfer whose connection closed waits for another one.
func (t *Transfers) orphanWait() time.Duration {
	return min(t.Grace, t.Idle)
}

func (t *Transfers) abandoned(id string, why error) {
	if t.Abandoned != nil {
		t.Abandoned(id, why)
	}
}

// Own ties a transfer to the connection a call about it came over. A call from another connection
// than the last one takes the transfer up again, which is how a host whose connection dropped and came
// back carries on; an id that names no transfer is ignored, since the call itself answers for it.
func (t *Transfers) Own(id string, conn context.Context) {
	if conn == nil {
		return
	}
	t.mu.Lock()
	item := t.items[id]
	t.mu.Unlock()
	if item != nil {
		item.adopt(conn)
	}
}

// owner follows the connection a transfer is driven over and calls orphaned when it closes, unless
// another connection has taken the transfer up meanwhile.
type owner struct {
	mu    sync.Mutex
	conn  context.Context
	gen   int
	ended chan struct{}
	once  sync.Once
}

func (o *owner) init() { o.ended = make(chan struct{}) }

func (o *owner) adopt(conn context.Context, orphaned func()) {
	o.mu.Lock()
	if o.conn == conn {
		o.mu.Unlock()
		return
	}
	o.conn = conn
	o.gen++
	gen := o.gen
	o.mu.Unlock()
	go func() {
		select {
		case <-conn.Done():
			o.mu.Lock()
			current := o.gen == gen
			o.mu.Unlock()
			if current {
				orphaned()
			}
		case <-o.ended:
		}
	}()
}

// release ends the watch: the transfer is over.
func (o *owner) release() { o.once.Do(func() { close(o.ended) }) }

// Counts are what a tree holds, as fs.unpack_finish and fs.pack_begin report it: the folder itself
// is not counted, a link is counted as a link, and Bytes is the regular files' sizes.
type Counts struct {
	Files    int   `json:"files"`
	Dirs     int   `json:"dirs"`
	Symlinks int   `json:"symlinks"`
	Bytes    int64 `json:"bytes"`
}

// Skipped is an entry fs.pack_begin left out, and why.
type Skipped struct {
	Path   string `json:"path"`
	Reason string `json:"reason"`
}

var errAborted = errors.New("the transfer was aborted")

func newTransferID() string {
	var b [12]byte
	_, _ = rand.Read(b[:])
	return hex.EncodeToString(b[:])
}

func (t *Transfers) add(id string, item transferItem) error {
	t.mu.Lock()
	defer t.mu.Unlock()
	running := 0
	for _, other := range t.items {
		if other.holdsSlot() {
			running++
		}
	}
	if running >= config.MaxTransfers {
		return fmt.Errorf("%w: %d folder transfers are running already", ErrBusy, config.MaxTransfers)
	}
	t.items[id] = item
	return nil
}

func (t *Transfers) take(id string) {
	t.mu.Lock()
	delete(t.items, id)
	t.mu.Unlock()
}

func (t *Transfers) unpackOf(id string) (*unpack, error) {
	t.mu.Lock()
	defer t.mu.Unlock()
	u, ok := t.items[id].(*unpack)
	if !ok || u.isFinished() {
		return nil, fmt.Errorf("%w: no unpack %q is running (finished, aborted or abandoned)", ErrNotFound, id)
	}
	u.touch()
	return u, nil
}

// anyUnpackOf finds an unpack running or finished and not yet released.
func (t *Transfers) anyUnpackOf(id string) (*unpack, error) {
	t.mu.Lock()
	defer t.mu.Unlock()
	u, ok := t.items[id].(*unpack)
	if !ok {
		return nil, fmt.Errorf("%w: no unpack %q is running or kept (released, aborted or abandoned)", ErrNotFound, id)
	}
	return u, nil
}

func (t *Transfers) packOf(id string) (*pack, error) {
	t.mu.Lock()
	defer t.mu.Unlock()
	p, ok := t.items[id].(*pack)
	if !ok {
		return nil, fmt.Errorf("%w: no pack %q is running (closed or abandoned)", ErrNotFound, id)
	}
	p.touch()
	return p, nil
}

// Close abandons every transfer, as at shutdown: what an unpack wrote is removed.
func (t *Transfers) Close() {
	t.mu.Lock()
	items := make([]transferItem, 0, len(t.items))
	for _, item := range t.items {
		items = append(items, item)
	}
	t.items = map[string]transferItem{}
	t.mu.Unlock()
	for _, item := range items {
		item.stop(errAborted)
	}
}

// idleTimer abandons a transfer nobody calls. Its own lock, because the timer's function may run
// before the goroutine that started it has stored it.
type idleTimer struct {
	mu    sync.Mutex
	timer *time.Timer
}

func (i *idleTimer) start(d time.Duration, f func()) {
	i.mu.Lock()
	i.timer = time.AfterFunc(d, f)
	i.mu.Unlock()
}

func (i *idleTimer) reset(d time.Duration) {
	i.mu.Lock()
	if i.timer != nil {
		i.timer.Reset(d)
	}
	i.mu.Unlock()
}

func (i *idleTimer) stop() {
	i.mu.Lock()
	if i.timer != nil {
		i.timer.Stop()
	}
	i.mu.Unlock()
}

// underHome reports whether a path, as written and as resolved, is strictly inside the home
// directory: where an unpack may make its folder.
func (f *FS) underHome(clean, real string) bool {
	homeReal := f.home
	if r, err := filepath.EvalSymlinks(f.home); err == nil {
		homeReal = r
	}
	inside := func(home, p string) bool { return p != home && under(home, p) }
	return (inside(f.home, clean) || inside(homeReal, clean)) && (inside(f.home, real) || inside(homeReal, real))
}

// entryName checks one name of a stream and returns it cleaned, with "/" separators; "" is the
// folder itself. A name that is absolute, climbs with `..` anywhere, or (on Windows) holds a
// backslash or a drive's colon is refused rather than cleaned into something harmless: a stream that
// carries one was not made by a sender that means well.
func entryName(name string) (string, error) {
	if len(name) > 4096 || strings.ContainsRune(name, 0) {
		return "", fmt.Errorf("%w: an entry name is too long or holds a NUL", ErrInvalid)
	}
	if foldPaths && strings.ContainsAny(name, `\:`) {
		return "", fmt.Errorf("%w: %q is not a portable name", ErrInvalid, name)
	}
	trimmed := strings.TrimSuffix(strings.TrimPrefix(name, "./"), "/")
	if trimmed == "" || trimmed == "." {
		return "", nil
	}
	if strings.HasPrefix(trimmed, "/") || filepath.IsAbs(trimmed) {
		return "", fmt.Errorf("%w: %q is absolute", ErrInvalid, name)
	}
	for _, seg := range strings.Split(trimmed, "/") {
		if seg == ".." {
			return "", fmt.Errorf("%w: %q climbs out of the folder", ErrInvalid, name)
		}
	}
	return path.Clean(trimmed), nil
}

// linkEscapes reports whether a link at rel (a cleaned name inside the folder) pointing at target
// would lead out of the folder, or is absolute.
func linkEscapes(rel, target string) bool {
	if target == "" || strings.ContainsRune(target, 0) || path.IsAbs(target) || filepath.IsAbs(filepath.FromSlash(target)) || filepath.VolumeName(filepath.FromSlash(target)) != "" {
		return true
	}
	if foldPaths && strings.ContainsAny(target, `\:`) {
		return true
	}
	joined := path.Clean(path.Join(path.Dir(rel), target))
	return joined == ".." || strings.HasPrefix(joined, "../")
}

// countTree counts what a folder holds, links not followed.
func countTree(root string) (Counts, error) {
	var c Counts
	err := filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if p == root {
			return nil
		}
		switch t := d.Type(); {
		case t.IsDir():
			c.Dirs++
		case t&fs.ModeSymlink != 0:
			c.Symlinks++
		case t.IsRegular():
			info, err := d.Info()
			if err != nil {
				return err
			}
			c.Files++
			c.Bytes += info.Size()
		}
		return nil
	})
	return c, err
}

// -- unpack ---------------------------------------------------------------------------------------

// unpack is one fs.unpack_*: the bytes of a tar stream written into a pipe, and a goroutine
// extracting them into the target as they come.
type unpack struct {
	t        *Transfers
	id       string
	target   string // as asked, cleaned
	real     string // its resolution when the transfer began
	created  bool   // the transfer made the folder, so a cleanup removes it rather than empties it
	declared int64
	entries  int
	owner    owner

	stateMu  sync.Mutex
	finished bool // the stream ended whole; the folder is kept until released, aborted or forgotten

	mu       sync.Mutex // one write at a time, in order
	pw       *io.PipeWriter
	streamed int64
	digest   hash.Hash
	done     chan struct{}
	err      error // the extraction's result, once done is closed
	idle     idleTimer
	stopOnce sync.Once
}

// UnpackStarted is fs.unpack_begin's answer.
type UnpackStarted struct {
	Transfer string `json:"transfer"`
	Path     string `json:"path"`
	Created  bool   `json:"created"`
}

// Unpacked is fs.unpack_finish's answer: the folder as it is on disk now, and the SHA-256 of the
// stream as received, for the sender to compare with what it sent.
type Unpacked struct {
	Counts
	SHA256 string `json:"sha256"`
	Path   string `json:"path"`
}

// UnpackBegin makes (or takes, when it is there and empty) the folder a stream will be unpacked
// into. total is the most the regular files of the stream may hold, entries the most entries.
func (t *Transfers) UnpackBegin(p string, total int64, entries int) (UnpackStarted, error) {
	if total < 0 || total > config.MaxTransferBytes {
		return UnpackStarted{}, fmt.Errorf("%w: total_bytes is 0..%d", ErrInvalid, config.MaxTransferBytes)
	}
	if entries < 0 || entries > config.MaxTransferEntries {
		return UnpackStarted{}, fmt.Errorf("%w: entries is 0..%d", ErrInvalid, config.MaxTransferEntries)
	}
	clean, real, exists, err := t.fs.checkRoot(p)
	if err != nil {
		return UnpackStarted{}, err
	}
	if !t.fs.underHome(clean, real) {
		return UnpackStarted{}, fmt.Errorf("%w: a folder is moved here only into a new folder inside the home directory (%s), not %s", ErrForbidden, t.fs.home, clean)
	}
	created := false
	if exists {
		info, err := os.Lstat(clean)
		if err != nil {
			return UnpackStarted{}, err
		}
		if !info.IsDir() || info.Mode()&fs.ModeSymlink != 0 {
			return UnpackStarted{}, fmt.Errorf("%w: %s exists and is not a plain folder", ErrInvalid, clean)
		}
		dir, err := openNoFollow(real, true)
		if err != nil {
			return UnpackStarted{}, err
		}
		names, err := dir.Readdirnames(1)
		dir.Close()
		if len(names) > 0 {
			return UnpackStarted{}, fmt.Errorf("%w: %s is not empty; a folder is moved only into a new or empty one", ErrForbidden, clean)
		}
		if err != nil && !errors.Is(err, io.EOF) {
			return UnpackStarted{}, err
		}
	} else {
		if err := os.MkdirAll(clean, 0o777); err != nil {
			return UnpackStarted{}, err
		}
		created = true
	}
	// Checked again as made: a parent swapped for a link between the check and the mkdir would put
	// the folder somewhere it may not be.
	_, real, _, err = t.fs.checkRoot(clean)
	if err == nil && !t.fs.underHome(clean, real) {
		err = fmt.Errorf("%w: %s resolves outside the home directory", ErrForbidden, clean)
	}
	if err != nil {
		if created {
			_ = os.Remove(clean)
		}
		return UnpackStarted{}, err
	}
	pr, pw := io.Pipe()
	u := &unpack{t: t, id: newTransferID(), target: clean, real: real, created: created, declared: total, entries: entries,
		pw: pw, digest: sha256.New(), done: make(chan struct{})}
	u.owner.init()
	if err := t.add(u.id, u); err != nil {
		if created {
			_ = os.Remove(clean)
		}
		return UnpackStarted{}, err
	}
	u.idle.start(t.Idle, u.expire)
	go func() {
		u.err = u.extract(pr)
		// A failed extraction closes the pipe with its error, so the next write returns it rather
		// than waiting for a reader that has gone.
		pr.CloseWithError(u.errOr(io.ErrClosedPipe))
		close(u.done)
	}()
	return UnpackStarted{Transfer: u.id, Path: clean, Created: created}, nil
}

func (u *unpack) errOr(fallback error) error {
	if u.err != nil {
		return u.err
	}
	return fallback
}

func (u *unpack) touch() { u.idle.reset(u.t.Idle) }

func (u *unpack) isFinished() bool {
	u.stateMu.Lock()
	defer u.stateMu.Unlock()
	return u.finished
}

func (u *unpack) holdsSlot() bool { return !u.isFinished() }

func (u *unpack) adopt(conn context.Context) {
	u.owner.adopt(conn, func() {
		// A finished copy waits for its release for the whole idle time whoever drives it: the host
		// that comes back after a restart is the one that knows whether it switched to it.
		if !u.isFinished() {
			u.idle.reset(u.t.orphanWait())
		}
	})
}

// expire is the idle timer's end: a running unpack is abandoned and cleaned up, a finished one only
// forgotten.
func (u *unpack) expire() {
	if u.isFinished() {
		u.forget()
		return
	}
	why := fmt.Errorf("the transfer was abandoned: no call came in time and the host that drove it is gone")
	u.t.abandoned(u.id, why)
	u.stop(why)
}

// stop ends the transfer without finishing it and removes what it wrote; a finished one is only
// forgotten, as at the daemon's shutdown, since the host may be working in it by now.
func (u *unpack) stop(reason error) {
	if u.isFinished() {
		u.forget()
		return
	}
	u.stopOnce.Do(func() {
		u.t.take(u.id)
		u.idle.stop()
		u.owner.release()
		u.pw.CloseWithError(reason)
		<-u.done
		u.cleanup()
	})
}

// discard removes a finished copy the host decided against.
func (u *unpack) discard() {
	u.stopOnce.Do(func() {
		u.t.take(u.id)
		u.idle.stop()
		u.owner.release()
		u.cleanup()
	})
}

// forget lets a finished copy go and leaves it where it is.
func (u *unpack) forget() {
	u.stopOnce.Do(func() {
		u.t.take(u.id)
		u.idle.stop()
		u.owner.release()
	})
}

// cleanup removes the partial folder: the folder itself when the transfer made it, its contents when
// it was there and empty. Only when the path still resolves to the folder the transfer wrote, so a
// link swapped in for it meanwhile is not followed into somebody else's files.
func (u *unpack) cleanup() {
	real, err := filepath.EvalSymlinks(u.target)
	if err != nil || real != u.real {
		return
	}
	if u.created {
		_ = os.RemoveAll(u.real)
		return
	}
	dir, err := openNoFollow(u.real, true)
	if err != nil {
		return
	}
	names, _ := dir.Readdirnames(-1)
	dir.Close()
	for _, name := range names {
		_ = os.RemoveAll(filepath.Join(u.real, name))
	}
}

// UnpackWrite feeds the next bytes of the stream. An extraction that failed answers with its error;
// the caller then aborts, which cleans up.
func (t *Transfers) UnpackWrite(id string, data []byte) (int64, error) {
	if len(data) > config.MaxFSWrite {
		return 0, fmt.Errorf("%w: one write carries at most %d bytes", ErrInvalid, config.MaxFSWrite)
	}
	u, err := t.unpackOf(id)
	if err != nil {
		return 0, err
	}
	u.mu.Lock()
	defer u.mu.Unlock()
	// Headers, a long name's extended header and a file's padding cost at most a few kilobytes an
	// entry beyond the files' bytes; a stream past that is not the one that was declared.
	if limit := u.declared + int64(u.entries+2)*16*512 + 1<<20; u.streamed+int64(len(data)) > limit {
		return u.streamed, fmt.Errorf("%w: the stream is longer than the %d bytes it declared", ErrInvalid, u.declared)
	}
	u.digest.Write(data)
	if _, err := u.pw.Write(data); err != nil {
		select {
		case <-u.done:
			return u.streamed, u.errOr(err)
		default:
		}
		return u.streamed, err
	}
	u.streamed += int64(len(data))
	return u.streamed, nil
}

// UnpackFinish ends the stream, waits for the extraction, and counts the folder as it now is on
// disk. A failure removes what was written.
func (t *Transfers) UnpackFinish(id string) (Unpacked, error) {
	u, err := t.unpackOf(id)
	if err != nil {
		return Unpacked{}, err
	}
	u.mu.Lock()
	u.pw.Close()
	<-u.done
	u.mu.Unlock()
	if u.err != nil {
		u.stop(u.err)
		return Unpacked{}, u.err
	}
	counts, err := countTree(u.real)
	if err != nil {
		u.stop(err)
		return Unpacked{}, err
	}
	u.stateMu.Lock()
	u.finished = true
	u.stateMu.Unlock()
	u.touch()
	return Unpacked{Counts: counts, SHA256: hex.EncodeToString(u.digest.Sum(nil)), Path: u.target}, nil
}

// UnpackAbort ends the transfer and removes what it wrote: a running one's partial folder, or a
// finished one the host has not released — the copy it decided against before switching to it.
func (t *Transfers) UnpackAbort(id string) error {
	u, err := t.anyUnpackOf(id)
	if err != nil {
		return err
	}
	if u.isFinished() {
		u.discard()
		return nil
	}
	u.stop(errAborted)
	return nil
}

// UnpackRelease lets a finished unpack go and leaves its folder as it is: the host works in it now.
func (t *Transfers) UnpackRelease(id string) error {
	u, err := t.anyUnpackOf(id)
	if err != nil {
		return err
	}
	if !u.isFinished() {
		return fmt.Errorf("%w: the unpack %q has not finished; abort it instead", ErrInvalid, id)
	}
	u.forget()
	return nil
}

// extract reads the stream and writes its entries under the folder.
func (u *unpack) extract(r io.Reader) error {
	tr := tar.NewReader(r)
	links := map[string]bool{}
	type dirMode struct {
		full string
		mode fs.FileMode
	}
	var dirs []dirMode
	var written int64
	seen := 0
	for {
		hdr, err := tr.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return fmt.Errorf("%w: the stream is not a tar archive: %v", ErrInvalid, err)
		}
		seen++
		if seen > u.entries {
			return fmt.Errorf("%w: the stream holds more than the %d entries it declared", ErrInvalid, u.entries)
		}
		rel, err := entryName(hdr.Name)
		if err != nil {
			return err
		}
		if rel == "" {
			if hdr.Typeflag != tar.TypeDir {
				return fmt.Errorf("%w: the folder itself can only be a directory entry", ErrInvalid)
			}
			continue
		}
		for dir := path.Dir(rel); dir != "."; dir = path.Dir(dir) {
			if links[dir] {
				return fmt.Errorf("%w: %s would be written through the link %s", ErrForbidden, rel, dir)
			}
		}
		if links[rel] {
			return fmt.Errorf("%w: %s is in the stream twice", ErrInvalid, rel)
		}
		full := filepath.Join(u.real, filepath.FromSlash(rel))
		perm := fs.FileMode(hdr.Mode).Perm()
		switch hdr.Typeflag {
		case tar.TypeDir:
			if err := os.MkdirAll(full, 0o700); err != nil {
				return err
			}
			if info, err := os.Lstat(full); err != nil || !info.IsDir() {
				return fmt.Errorf("%w: %s is not a directory", ErrInvalid, rel)
			}
			dirs = append(dirs, dirMode{full, perm | 0o700})
		case tar.TypeReg, '\x00':
			if hdr.Size < 0 || written+hdr.Size > u.declared {
				return fmt.Errorf("%w: the stream carries more than the %d bytes it declared", ErrInvalid, u.declared)
			}
			if err := os.MkdirAll(filepath.Dir(full), 0o700); err != nil {
				return err
			}
			file, err := openForWrite(full, true)
			if err != nil {
				return err
			}
			n, err := io.Copy(file, tr)
			written += n
			if err == nil && n != hdr.Size {
				err = fmt.Errorf("%w: %s is shorter than its header says", ErrInvalid, rel)
			}
			if err == nil {
				err = file.Chmod(perm | 0o600)
			}
			if cerr := file.Close(); err == nil {
				err = cerr
			}
			if err != nil {
				return err
			}
			_ = os.Chtimes(full, hdr.ModTime, hdr.ModTime)
		case tar.TypeSymlink:
			if linkEscapes(rel, hdr.Linkname) {
				return fmt.Errorf("%w: the link %s points out of the folder (%s)", ErrForbidden, rel, hdr.Linkname)
			}
			if err := os.MkdirAll(filepath.Dir(full), 0o700); err != nil {
				return err
			}
			if err := os.Symlink(filepath.FromSlash(hdr.Linkname), full); err != nil {
				return err
			}
			links[rel] = true
		default:
			return fmt.Errorf("%w: %s is an entry of a kind a folder is not moved with (%q)", ErrInvalid, rel, hdr.Typeflag)
		}
	}
	// Modes last, so a folder the stream marks read-only still took its files.
	for i := len(dirs) - 1; i >= 0; i-- {
		_ = os.Chmod(dirs[i].full, dirs[i].mode)
	}
	// The end of an archive is padded to a record; what follows the end marker is read and dropped.
	_, _ = io.Copy(io.Discard, r)
	return nil
}

// -- pack -----------------------------------------------------------------------------------------

type packEntry struct {
	rel   string
	kind  string // "dir", "file", "symlink"
	size  int64
	perm  fs.FileMode
	mtime time.Time
	link  string
}

// pack is one fs.pack_*: the folder's manifest taken at the start, and a goroutine writing the tar
// stream of it into a pipe the reads drain.
type pack struct {
	t        *Transfers
	id       string
	root     string
	real     string
	manifest []packEntry
	skipped  int
	counts   Counts
	owner    owner

	mu       sync.Mutex
	pr       *io.PipeReader
	digest   hash.Hash
	eof      bool
	done     chan struct{}
	err      error
	idle     idleTimer
	stopOnce sync.Once
}

// PackStarted is fs.pack_begin's answer: what will be sent, and what is left out (at most
// MaxTransferSkipped are named; SkippedCount is all of them).
type PackStarted struct {
	Transfer string `json:"transfer"`
	Counts
	Skipped      []Skipped `json:"skipped"`
	SkippedCount int       `json:"skipped_count"`
}

// PackChunk is one fs.pack_read: the next bytes; at the end, EOF with the stream's SHA-256.
type PackChunk struct {
	Data   []byte `json:"data_b64"`
	EOF    bool   `json:"eof"`
	SHA256 string `json:"sha256,omitempty"`
}

// PackBegin takes the manifest of a folder under the roots and starts its stream.
func (t *Transfers) PackBegin(p string) (PackStarted, error) {
	clean, real, exists, err := t.fs.resolve(p)
	if err != nil {
		return PackStarted{}, err
	}
	if !exists {
		return PackStarted{}, fmt.Errorf("%w: %s", ErrNotFound, clean)
	}
	if reason := t.fs.rootRefusal(clean, real); reason != "" {
		return PackStarted{}, fmt.Errorf("%w: %s cannot be moved whole: it %s", ErrForbidden, clean, reason)
	}
	info, err := os.Stat(real)
	if err != nil {
		return PackStarted{}, err
	}
	if !info.IsDir() {
		return PackStarted{}, fmt.Errorf("%w: %s is not a folder", ErrInvalid, clean)
	}
	pk := &pack{t: t, id: newTransferID(), root: clean, real: real, digest: sha256.New(), done: make(chan struct{})}
	pk.owner.init()
	started := PackStarted{Skipped: []Skipped{}}
	skip := func(rel, reason string) {
		pk.skipped++
		if len(started.Skipped) < config.MaxTransferSkipped {
			started.Skipped = append(started.Skipped, Skipped{Path: rel, Reason: reason})
		}
	}
	err = filepath.WalkDir(real, func(full string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if full == real {
			return nil
		}
		relOS, err := filepath.Rel(real, full)
		if err != nil {
			return err
		}
		rel := filepath.ToSlash(relOS)
		if t.fs.denied(full) || t.fs.denied(filepath.Join(clean, relOS)) || t.fs.sealedPath(full) {
			skip(rel, "denied")
			if d.IsDir() {
				return filepath.SkipDir
			}
			return nil
		}
		info, err := d.Info()
		if err != nil {
			return err
		}
		entry := packEntry{rel: rel, perm: info.Mode().Perm(), mtime: info.ModTime()}
		switch mode := info.Mode(); {
		case mode.IsDir():
			entry.kind = "dir"
			pk.counts.Dirs++
		case mode.IsRegular():
			entry.kind, entry.size = "file", info.Size()
			pk.counts.Files++
			pk.counts.Bytes += info.Size()
		case mode&fs.ModeSymlink != 0:
			target, err := os.Readlink(full)
			if err != nil {
				return err
			}
			target = filepath.ToSlash(target)
			if linkEscapes(rel, target) {
				skip(rel, "link outside the folder")
				return nil
			}
			entry.kind, entry.link = "symlink", target
			pk.counts.Symlinks++
		default:
			skip(rel, "special file")
			return nil
		}
		pk.manifest = append(pk.manifest, entry)
		if len(pk.manifest) > config.MaxTransferEntries {
			return fmt.Errorf("%w: the folder holds more than %d entries", ErrInvalid, config.MaxTransferEntries)
		}
		if pk.counts.Bytes > config.MaxTransferBytes {
			return fmt.Errorf("%w: the folder holds more than %d bytes", ErrInvalid, config.MaxTransferBytes)
		}
		return nil
	})
	if err != nil {
		return PackStarted{}, err
	}
	pr, pw := io.Pipe()
	pk.pr = pr
	if err := t.add(pk.id, pk); err != nil {
		return PackStarted{}, err
	}
	pk.idle.start(t.Idle, func() {
		why := fmt.Errorf("the transfer was abandoned: no call came in time and the host that drove it is gone")
		t.abandoned(pk.id, why)
		pk.stop(why)
	})
	go func() {
		err := pk.write(pw)
		pw.CloseWithError(err)
		pk.err = err
		close(pk.done)
	}()
	started.Transfer, started.Counts, started.SkippedCount = pk.id, pk.counts, pk.skipped
	return started, nil
}

func (pk *pack) touch() { pk.idle.reset(pk.t.Idle) }

func (pk *pack) holdsSlot() bool { return true }

func (pk *pack) adopt(conn context.Context) {
	pk.owner.adopt(conn, func() { pk.idle.reset(pk.t.orphanWait()) })
}

func (pk *pack) stop(reason error) {
	pk.stopOnce.Do(func() {
		pk.t.take(pk.id)
		pk.idle.stop()
		pk.owner.release()
		pk.pr.CloseWithError(reason)
		<-pk.done
	})
}

// write sends the manifest as a tar stream. A file is opened without following a link and must
// still be a regular file of at least the size the manifest took: one that shrank or was swapped
// changed under the move, and the stream fails rather than carry something else.
func (pk *pack) write(w io.Writer) error {
	tw := tar.NewWriter(w)
	for _, e := range pk.manifest {
		// Whole seconds: a fraction would cost every entry an extended header, and the receiving side
		// keeps seconds anyway.
		hdr := &tar.Header{Name: e.rel, Mode: int64(e.perm), ModTime: e.mtime.Truncate(time.Second), Format: tar.FormatPAX}
		switch e.kind {
		case "dir":
			hdr.Typeflag, hdr.Name = tar.TypeDir, e.rel+"/"
		case "symlink":
			hdr.Typeflag, hdr.Linkname = tar.TypeSymlink, e.link
		case "file":
			hdr.Typeflag, hdr.Size = tar.TypeReg, e.size
		}
		if err := tw.WriteHeader(hdr); err != nil {
			return err
		}
		if e.kind != "file" {
			continue
		}
		full := filepath.Join(pk.real, filepath.FromSlash(e.rel))
		file, err := openNoFollow(full, false)
		if err != nil {
			return fmt.Errorf("%s changed during the move: %w", e.rel, err)
		}
		st, err := file.Stat()
		if err == nil && !st.Mode().IsRegular() {
			err = fmt.Errorf("%s is no longer a regular file", e.rel)
		}
		if err == nil {
			_, err = io.CopyN(tw, file, e.size)
			if errors.Is(err, io.EOF) {
				err = fmt.Errorf("%s shrank during the move", e.rel)
			}
		}
		file.Close()
		if err != nil {
			return err
		}
	}
	return tw.Close()
}

// PackRead returns up to max bytes of the stream (at most one reply's worth).
func (t *Transfers) PackRead(id string, max int) (PackChunk, error) {
	n, err := clampMax(max)
	if err != nil {
		return PackChunk{}, err
	}
	pk, err := t.packOf(id)
	if err != nil {
		return PackChunk{}, err
	}
	pk.mu.Lock()
	defer pk.mu.Unlock()
	if pk.eof {
		return PackChunk{EOF: true, SHA256: hex.EncodeToString(pk.digest.Sum(nil))}, nil
	}
	buf := make([]byte, n)
	got := 0
	for got < n {
		k, err := pk.pr.Read(buf[got:])
		got += k
		if errors.Is(err, io.EOF) {
			<-pk.done
			pk.eof = true
			break
		}
		if err != nil {
			pk.digest.Write(buf[:got])
			return PackChunk{}, err
		}
	}
	pk.digest.Write(buf[:got])
	out := PackChunk{Data: buf[:got], EOF: pk.eof}
	if pk.eof {
		out.SHA256 = hex.EncodeToString(pk.digest.Sum(nil))
	}
	return out, nil
}

// PackClose ends a pack, whether or not it was read to the end. The folder is left as it is.
func (t *Transfers) PackClose(id string) error {
	pk, err := t.packOf(id)
	if err != nil {
		return err
	}
	pk.stop(errAborted)
	return nil
}

// PackRemove deletes the folder a pack has sent whole, and ends the pack. Refused when the stream
// was not read to its end, when anything was left out of it (it would be deleted unsent), or when
// the folder is no longer what was sent: an entry added, removed, resized or touched since.
func (t *Transfers) PackRemove(id string) error {
	pk, err := t.packOf(id)
	if err != nil {
		return err
	}
	pk.mu.Lock()
	complete := pk.eof && pk.err == nil
	pk.mu.Unlock()
	if !complete {
		return fmt.Errorf("%w: the folder was not sent whole, so it is kept", ErrForbidden)
	}
	if pk.skipped > 0 {
		return fmt.Errorf("%w: %d entries of the folder were not sent, so it is kept", ErrForbidden, pk.skipped)
	}
	defer pk.stop(errAborted)
	real, err := filepath.EvalSymlinks(pk.root)
	if err != nil || real != pk.real {
		return fmt.Errorf("%w: %s no longer resolves to the folder that was sent, so it is kept", ErrForbidden, pk.root)
	}
	if err := pk.unchanged(); err != nil {
		return fmt.Errorf("%w: %v, so it is kept", ErrForbidden, err)
	}
	return os.RemoveAll(pk.real)
}

// unchanged compares the folder with the manifest the stream was made from.
func (pk *pack) unchanged() error {
	want := make(map[string]packEntry, len(pk.manifest))
	for _, e := range pk.manifest {
		want[e.rel] = e
	}
	seen := 0
	err := filepath.WalkDir(pk.real, func(full string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if full == pk.real {
			return nil
		}
		relOS, err := filepath.Rel(pk.real, full)
		if err != nil {
			return err
		}
		rel := filepath.ToSlash(relOS)
		e, ok := want[rel]
		if !ok {
			return fmt.Errorf("%s appeared after the folder was sent", rel)
		}
		seen++
		info, err := d.Info()
		if err != nil {
			return err
		}
		switch e.kind {
		case "dir":
			if !info.IsDir() {
				return fmt.Errorf("%s changed after the folder was sent", rel)
			}
		case "file":
			if !info.Mode().IsRegular() || info.Size() != e.size || !info.ModTime().Equal(e.mtime) {
				return fmt.Errorf("%s changed after the folder was sent", rel)
			}
		case "symlink":
			target, err := os.Readlink(full)
			if err != nil || filepath.ToSlash(target) != e.link {
				return fmt.Errorf("%s changed after the folder was sent", rel)
			}
		}
		return nil
	})
	if err != nil {
		return err
	}
	if seen != len(pk.manifest) {
		return fmt.Errorf("%d entries went missing after the folder was sent", len(pk.manifest)-seen)
	}
	return nil
}
